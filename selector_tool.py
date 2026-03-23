"""
SelectorTool
============
A selection-painter panel for Maya rigs.

Displays scene objects in a filterable tree grouped by selection sets.
Click-and-drag across items to paint-select them. Selecting a group
header selects all of its children. Bidirectional sync keeps the list
and viewport selection in lockstep.

Launch from Script Editor:
    from my_tools.selector_tool import SelectorTool
    SelectorTool.show()

Or via the module-level helper:
    import my_tools.selector_tool
    my_tools.selector_tool.show()
"""

import logging

import maya.cmds as cmds

try:
    from PySide2 import QtWidgets, QtCore, QtGui
    from shiboken2 import isValid
except ImportError:
    from PySide6 import QtWidgets, QtCore, QtGui
    from shiboken6 import isValid

from .base import WorkspaceToolBase

log = logging.getLogger(__name__)
log.setLevel(logging.DEBUG)


# ---------------------------------------------------------------------------
#   Delegate that tints group headers based on child selection state
# ---------------------------------------------------------------------------

class _GroupTintDelegate(QtWidgets.QStyledItemDelegate):
    """
    Draws a coloured background behind group-header rows to indicate
    how many of their children are selected:
      - no children selected  → default (no tint)
      - some children selected → dim blue
      - all children selected  → brighter blue
    """

    COLOR_SOME = QtGui.QColor(70, 120, 180, 90)   # dim blue
    COLOR_ALL  = QtGui.QColor(90, 150, 220, 140)   # brighter blue

    def paint(self, painter, option, index):
        # Only tint group headers (UserRole+1 == True)
        item = self.parent().itemFromIndex(index)
        if item and item.data(0, QtCore.Qt.UserRole + 1):
            child_count = item.childCount()
            if child_count:
                selected = sum(
                    1 for i in range(child_count) if item.child(i).isSelected()
                )
                if selected == child_count:
                    color = self.COLOR_ALL
                elif selected > 0:
                    color = self.COLOR_SOME
                else:
                    color = None

                if color:
                    painter.save()
                    painter.fillRect(option.rect, color)
                    painter.restore()

        super().paint(painter, option, index)


# ---------------------------------------------------------------------------
#   Custom tree widget with paint-select (click-drag) support
# ---------------------------------------------------------------------------

class _PaintSelectTree(QtWidgets.QTreeWidget):
    """
    QTreeWidget subclass that lets the user click-and-drag across items
    to select a contiguous range.  The range is defined by the anchor
    (where the mouse was pressed) and the current item under the cursor.
    Dragging back shrinks the selection — standard list-box behaviour.
    """

    paintSelectFinished = QtCore.Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self._painting = False
        self._anchor_key = None  # long name of the anchor item (survives tree rebuilds)
        self._anchor_item = None  # direct QTreeWidgetItem ref (fast, but invalidated on rebuild)
        self._drag_deselecting = False  # True when Ctrl+click on selected item
        self._pre_drag_selection = set()  # items selected before this drag
        self._handled = False  # True when we handled press ourselves
        self._sync_callback = None  # set by SelectorTool for tree→Maya sync
        self.setSelectionMode(QtWidgets.QAbstractItemView.ExtendedSelection)
        self.setDragEnabled(False)
        self.setAcceptDrops(False)
        self.setRootIsDecorated(True)
        self.setHeaderHidden(True)
        self.setIndentation(16)
        self.setItemDelegate(_GroupTintDelegate(self))

    # -- events ------------------------------------------------------------

    def mousePressEvent(self, event):
        if event.button() == QtCore.Qt.LeftButton:
            item = self.itemAt(event.pos())

            # --- empty space: clear selection ---
            if not item:
                self._handled = True
                self._painting = False
                self.blockSignals(True)
                self.clearSelection()
                self.blockSignals(False)
                if self._sync_callback:
                    self._sync_callback([])
                self._update_group_tints()
                return

            # --- group header: select/toggle children ---
            if item.data(0, QtCore.Qt.UserRole + 1):
                # Let clicks on the expand/collapse arrow pass through to Qt
                item_rect = self.visualItemRect(item)
                arrow_width = self.indentation()
                indent_level = 0
                parent = item.parent()
                while parent:
                    indent_level += 1
                    parent = parent.parent()
                arrow_x = indent_level * arrow_width
                if event.pos().x() < arrow_x + arrow_width:
                    self._handled = False
                    super().mousePressEvent(event)
                    return

                self._handled = True
                self._painting = False
                mods = event.modifiers()
                ctrl = mods & QtCore.Qt.ControlModifier
                shift = mods & QtCore.Qt.ShiftModifier

                self.blockSignals(True)

                # Collect keys for this group's children and current selection
                leaves = self._leaf_items()
                children = [item.child(i) for i in range(item.childCount())]
                log.debug("group click: group=%s ctrl=%s shift=%s "
                          "childCount=%d children_texts=%s",
                          item.text(0), bool(ctrl), bool(shift),
                          item.childCount(),
                          [c.text(0) for c in children])
                group_keys = {self._item_key(c) for c in children} - {None}
                current_keys = {self._item_key(l) for l in leaves
                                if l.isSelected()} - {None}
                log.debug("group click: group_keys=%s current_keys=%s",
                          group_keys, current_keys)

                if ctrl:
                    # Ctrl+click group: toggle — remove keys if any
                    # children selected, otherwise add them
                    any_selected = bool(group_keys & current_keys)
                    log.debug("group ctrl: any_selected=%s result_keys=%s",
                              any_selected,
                              current_keys - group_keys if any_selected
                              else current_keys | group_keys)
                    if any_selected:
                        selected_keys_set = current_keys - group_keys
                    else:
                        selected_keys_set = current_keys | group_keys
                elif shift:
                    # Shift+click group: add all children to selection
                    selected_keys_set = current_keys | group_keys
                else:
                    # Plain click group: exclusive select all children
                    selected_keys_set = group_keys

                # Apply selection to all leaves (clone-aware)
                for leaf in leaves:
                    key = self._item_key(leaf)
                    leaf.setSelected(bool(key and key in selected_keys_set))

                self.blockSignals(False)
                if self._sync_callback:
                    self._sync_callback(list(selected_keys_set))
                self._update_group_tints()
                return

            # --- leaf item ---
            mods = event.modifiers()
            shift = mods & QtCore.Qt.ShiftModifier
            ctrl = mods & QtCore.Qt.ControlModifier

            self._handled = True
            log.debug("press: item=%s shift=%s ctrl=%s anchor_key=%s",
                      item.text(0), bool(shift), bool(ctrl),
                      self._anchor_key)

            if shift and self._anchor_key:
                # Shift+click: select range from anchor, no drag
                self._pre_drag_selection = set()
                self._drag_deselecting = False
                self._painting = False
                log.debug("shift-click: range %s -> %s",
                          self._anchor_key, item.text(0))
                self._apply_range(item)
                return

            self._painting = True
            if not ctrl:
                # Plain click — set new anchor
                self._anchor_key = self._item_key(item)
                self._anchor_item = item
                self._pre_drag_selection = set()
                self._drag_deselecting = False
                log.debug("plain-click: new anchor=%s", item.text(0))
            else:
                # Ctrl+click — new anchor at clicked item, add/remove mode
                self._anchor_key = self._item_key(item)
                self._anchor_item = item
                self._pre_drag_selection = set(self.selectedItems())
                self._drag_deselecting = item.isSelected()
                log.debug("ctrl-click: anchor=%s pre_drag=%s desel=%s",
                          item.text(0),
                          [i.text(0) for i in self._pre_drag_selection],
                          self._drag_deselecting)
            self._apply_range(item)
            return
        self._handled = False
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if self._painting:
            item = self.itemAt(event.pos())
            if item and not item.data(0, QtCore.Qt.UserRole + 1):
                self._apply_range(item)
            return
        if self._handled:
            return  # suppress Qt's default during shift-click (no paint)
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        if event.button() == QtCore.Qt.LeftButton and self._handled:
            if self._painting:
                self._painting = False
                self._drag_deselecting = False
                self._pre_drag_selection = set()
                self.paintSelectFinished.emit()
            self._handled = False
            return
        self._handled = False
        super().mouseReleaseEvent(event)

    # -- helpers -----------------------------------------------------------

    def _update_group_tints(self):
        """Schedule a viewport repaint so group header tints refresh."""
        self.viewport().update()

    @staticmethod
    def _item_key(item):
        """Return the long name stored on *item* (used as a stable identity)."""
        return item.data(0, QtCore.Qt.UserRole)

    def _leaf_items(self):
        """Return all visible non-group items in visual order."""
        items = []
        iterator = QtWidgets.QTreeWidgetItemIterator(
            self, QtWidgets.QTreeWidgetItemIterator.NoChildren
        )
        while iterator.value():
            item = iterator.value()
            if not item.data(0, QtCore.Qt.UserRole + 1):
                items.append(item)
            iterator += 1
        return items

    def _find_anchor(self, leaves):
        """Return the leaf item matching the anchor, or *None*.

        Prefers the direct item reference (handles duplicates across groups)
        and falls back to key-based lookup (survives tree rebuilds).
        """
        # Fast path: direct reference still valid and present in current leaves
        if self._anchor_item is not None and self._anchor_item in leaves:
            return self._anchor_item
        # Fallback: key-based lookup (e.g. after tree rebuild)
        if not self._anchor_key:
            return None
        for item in leaves:
            if self._item_key(item) == self._anchor_key:
                self._anchor_item = item  # cache for next time
                return item
        return None

    def _apply_range(self, end_item):
        """Select (or deselect) the contiguous range from anchor to *end_item*.

        After computing the range, all clones (items sharing the same key in
        other groups) are brought into the same selected/deselected state so
        that duplicates always stay in sync.
        """
        leaves = self._leaf_items()
        anchor_item = self._find_anchor(leaves)
        if not anchor_item:
            log.debug("_apply_range: anchor_key %r not found in leaves", self._anchor_key)
            return
        try:
            anchor_idx = leaves.index(anchor_item)
            end_idx = leaves.index(end_item)
        except ValueError:
            log.debug("_apply_range: end item not in leaves")
            return
        log.debug("_apply_range: anchor_idx=%d end_idx=%d leaves=%d",
                   anchor_idx, end_idx, len(leaves))

        lo, hi = sorted((anchor_idx, end_idx))
        range_items = leaves[lo:hi + 1]

        # Collect keys so clone-aware add/remove operates on identity, not position
        range_keys = {self._item_key(i) for i in range_items} - {None}
        pre_drag_keys = {self._item_key(i) for i in self._pre_drag_selection} - {None}

        self.blockSignals(True)
        self.clearSelection()

        if self._drag_deselecting:
            selected_keys_set = pre_drag_keys - range_keys
        else:
            selected_keys_set = range_keys | pre_drag_keys

        # Second pass: apply selection, including clones that share a key
        for item in leaves:
            key = self._item_key(item)
            item.setSelected(bool(key and key in selected_keys_set))

        self.blockSignals(False)
        # Set focus rect without changing selection state
        idx = self.indexFromItem(end_item)
        self.selectionModel().setCurrentIndex(
            idx, QtCore.QItemSelectionModel.NoUpdate
        )

        if self._sync_callback:
            self._sync_callback(list(selected_keys_set))
        self._update_group_tints()


# ---------------------------------------------------------------------------
#   SelectorTool
# ---------------------------------------------------------------------------

class SelectorTool(WorkspaceToolBase):

    TOOL_NAME = "SelectorTool"
    TOOL_TITLE = "Selector"
    DEFAULT_WIDTH = 250
    DEFAULT_HEIGHT = 600

    UNGROUPED_LABEL = "Ungrouped"

    def __init__(self, parent=None):
        self._syncing = False  # guard against selection-sync loops
        self._script_jobs = []
        super().__init__(parent)

    # ── UI ────────────────────────────────────────────────────────────────

    def build_ui(self):
        # --- menu bar ---
        menu_bar = QtWidgets.QMenuBar(self)
        options_menu = menu_bar.addMenu("Options")
        self._show_second_action = options_menu.addAction("Show Second List")
        self._show_second_action.setCheckable(True)
        self._show_second_action.setChecked(self._pref_bool("showSecondList"))
        self._show_second_action.toggled.connect(self._toggle_second_list)
        self.main_layout.setMenuBar(menu_bar)

        # --- type filter ---
        self.filter_edit = QtWidgets.QLineEdit()
        self.filter_edit.setPlaceholderText("Filter types  (e.g. transform | -camera)")
        self.filter_edit.setClearButtonEnabled(True)
        self.filter_edit.setText(self._pref_string("filter1"))
        self.filter_edit.returnPressed.connect(self._refresh)
        self.main_layout.addWidget(self.filter_edit)

        # debounce timer so typing doesn't hammer the scene query
        self._filter_timer = QtCore.QTimer(self)
        self._filter_timer.setSingleShot(True)
        self._filter_timer.setInterval(400)
        self._filter_timer.timeout.connect(self._refresh)
        self.filter_edit.textChanged.connect(self._restart_filter_timer)
        self.filter_edit.textChanged.connect(lambda t: self._set_pref("filter1", t))

        # --- tree ---
        self.tree = _PaintSelectTree()
        self.tree._sync_callback = self._apply_maya_selection
        self.tree.itemExpanded.connect(self._on_group_expanded)
        self.tree.itemCollapsed.connect(self._on_group_collapsed)
        self.tree.setContextMenuPolicy(QtCore.Qt.CustomContextMenu)
        self.tree.customContextMenuRequested.connect(self._show_context_menu)
        self.main_layout.addWidget(self.tree)

        # --- second filter + tree (hidden by default) ---
        show_second = self._pref_bool("showSecondList")
        self.filter_edit2 = QtWidgets.QLineEdit()
        self.filter_edit2.setPlaceholderText("Filter types  (e.g. joint | transform)")
        self.filter_edit2.setClearButtonEnabled(True)
        self.filter_edit2.setText(self._pref_string("filter2"))
        self.filter_edit2.setVisible(show_second)
        self.filter_edit2.textChanged.connect(lambda t: self._set_pref("filter2", t))
        self.main_layout.addWidget(self.filter_edit2)

        self.tree2 = _PaintSelectTree()
        self.tree2.setVisible(show_second)
        self.main_layout.addWidget(self.tree2)

        # --- status ---
        self.status_label = QtWidgets.QLabel("0 items")
        self.status_label.setStyleSheet(
            "color: grey; font-size: 10px; padding: 2px;"
        )
        self.main_layout.addWidget(self.status_label)

        # --- style tweaks for tree group headers ---
        self.tree.setStyleSheet("""
            QTreeWidget::item { padding: 1px 0px; }
        """)

        # Defer scene queries and scriptJobs until the workspaceControl
        # is fully initialised (avoids errors during __init__).
        cmds.evalDeferred(self._deferred_init)

    def _deferred_init(self):
        """Called via evalDeferred so the workspaceControl is fully ready."""
        if not isValid(self):
            return
        self._refresh()
        self._install_script_jobs()

    # ── Options / preferences ────────────────────────────────────────────

    def _toggle_second_list(self, checked):
        self._set_pref("showSecondList", checked)
        self.filter_edit2.setVisible(checked)
        self.tree2.setVisible(checked)

    # ── Filter helpers ────────────────────────────────────────────────────

    def _restart_filter_timer(self):
        self._filter_timer.start()

    @staticmethod
    def _resolve_types(token):
        """Resolve a user token to a list of Maya node types.

        Exact matches are returned directly.  If no exact match exists,
        all registered node types whose name contains *token* (case-
        insensitive) are returned — e.g. ``cam`` → ``[camera, ...]``.
        """
        all_types = cmds.allNodeTypes() or []
        # Exact match (case-sensitive, as Maya types are)
        if token in all_types:
            return [token]
        # Substring / prefix fallback (case-insensitive)
        lower = token.lower()
        matches = [t for t in all_types if lower in t.lower()]
        return matches

    _SHAPE_TOKEN = "shape"

    def _parsed_types(self):
        """Return (include, exclude, show_shapes) from the filter.

        Tokens prefixed with ``-`` are exclusions.  The special token
        ``shape`` is not treated as a node type — instead it sets
        *show_shapes* to ``True`` so the caller can include shape nodes
        alongside their transforms.

        Example: ``nurbs | shape | -cam``
        """
        raw = self.filter_edit.text().strip()
        if not raw:
            return [], [], False
        include = []
        exclude = []
        show_shapes = False
        for t in raw.split("|"):
            t = t.strip()
            if not t:
                continue
            if t.startswith("-"):
                name = t[1:].strip()
                if name:
                    exclude.extend(self._resolve_types(name))
            elif t.lower() == self._SHAPE_TOKEN:
                show_shapes = True
            else:
                include.extend(self._resolve_types(t))
        return include, exclude, show_shapes

    # ── Custom group persistence (JSON config) ────────────────────────────

    _CONFIG_FILENAME = "selectorTool_config.json"

    def _load_custom_groups(self):
        """Return custom groups for the current scene. {name: [long_names]}"""
        return self._load_scene_config()

    def _save_custom_groups(self, groups):
        """Save custom groups for the current scene."""
        self._save_scene_config(groups)

    # ── Tree-item factories ─────────────────────────────────────────────

    _CUSTOM_GROUP_ROLE = QtCore.Qt.UserRole + 2  # True for custom groups

    @staticmethod
    def _make_leaf_item(long_name):
        """Create a leaf QTreeWidgetItem for a scene node."""
        short = long_name.rsplit("|", 1)[-1]
        item = QtWidgets.QTreeWidgetItem([short])
        item.setData(0, QtCore.Qt.UserRole, long_name)
        item.setData(0, QtCore.Qt.UserRole + 1, False)
        return item

    @classmethod
    def _make_group_header(cls, label, is_custom=False):
        """Create a bold group-header QTreeWidgetItem."""
        item = QtWidgets.QTreeWidgetItem([label])
        item.setData(0, QtCore.Qt.UserRole + 1, True)  # is_group flag
        item.setData(0, cls._CUSTOM_GROUP_ROLE, is_custom)
        font = item.font(0)
        font.setBold(True)
        item.setFont(0, font)
        return item

    # ── Scene query & tree build ──────────────────────────────────────────

    def _refresh(self):
        """Re-scan the scene and rebuild the tree."""
        if not isValid(self):
            return
        self._filter_timer.stop()

        # Snapshot Maya selection before clearing so we can restore highlights.
        prev_sel = set(cmds.ls(selection=True, long=True) or [])

        self._syncing = True
        try:
            self.tree._anchor_item = None  # invalidate; _find_anchor will re-resolve from key
            self.tree.blockSignals(True)
            self.tree.clear()
            self.tree.blockSignals(False)

            include, exclude, show_shapes = self._parsed_types()
            if not include:
                self.status_label.setText("Enter a type to filter")
                return

            # Gather all matching nodes
            raw_matches = set()
            for t in include:
                try:
                    raw_matches.update(cmds.ls(type=t, long=True) or [])
                except RuntimeError:
                    pass  # invalid type name — skip

            # Subtract excluded types
            for t in exclude:
                try:
                    raw_matches -= set(cmds.ls(type=t, long=True) or [])
                except RuntimeError:
                    pass

            # Resolve shape nodes to their transform parents.
            # If 'shape' token is present, keep shape nodes too.
            matching = set()
            for node in raw_matches:
                node_type = cmds.nodeType(node)
                is_shape = False
                try:
                    is_shape = cmds.objectType(node, isAType="shape")
                except RuntimeError:
                    pass
                if is_shape:
                    parent = cmds.listRelatives(node, parent=True, fullPath=True)
                    if parent:
                        matching.add(parent[0])
                    if show_shapes:
                        matching.add(node)
                else:
                    matching.add(node)

            if not matching:
                self.status_label.setText("0 items")
                return

            # ---- Custom groups (take priority) ----
            custom_groups = self._load_custom_groups()
            assigned = set()

            item_count = 0

            for grp_name in sorted(custom_groups):
                nodes = [n for n in custom_groups[grp_name] if n in matching]
                if not nodes:
                    continue
                nodes.sort(key=lambda n: n.rsplit("|", 1)[-1])
                assigned.update(nodes)
                group_item = self._make_group_header(grp_name, is_custom=True)
                self.tree.addTopLevelItem(group_item)
                for long_name in nodes:
                    group_item.addChild(self._make_leaf_item(long_name))
                    item_count += 1
                group_item.setExpanded(True)

            # ---- Selection-set groups (for remaining items) ----
            remaining = matching - assigned
            all_sets = cmds.ls(type="objectSet") or []
            default_sets = {
                "defaultLightSet", "defaultObjectSet",
                "initialParticleSE", "initialShadingGroup",
            }
            user_sets = [
                s for s in all_sets
                if s not in default_sets
                and not cmds.objectType(s, isAType="shadingEngine")
            ]

            for s in sorted(user_sets):
                members = cmds.sets(s, q=True, nodesOnly=True) or []
                long_members = []
                for m in members:
                    long_members.extend(cmds.ls(m, long=True) or [])
                group_nodes = sorted(
                    [n for n in long_members if n in remaining],
                    key=lambda n: n.rsplit("|", 1)[-1],
                )
                if not group_nodes:
                    continue
                assigned.update(group_nodes)
                group_item = self._make_group_header(s, is_custom=False)
                self.tree.addTopLevelItem(group_item)
                for long_name in group_nodes:
                    group_item.addChild(self._make_leaf_item(long_name))
                    item_count += 1
                group_item.setExpanded(True)

            # ---- Ungrouped section ----
            ungrouped = sorted(
                matching - assigned,
                key=lambda n: n.rsplit("|", 1)[-1],
            )
            if ungrouped:
                ug_item = self._make_group_header(self.UNGROUPED_LABEL, is_custom=False)
                self.tree.addTopLevelItem(ug_item)
                for long_name in ungrouped:
                    ug_item.addChild(self._make_leaf_item(long_name))
                    item_count += 1
                ug_item.setExpanded(True)

            self.status_label.setText(f"{item_count} items")

            # Restore selection highlight from snapshot taken before clear.
            if prev_sel:
                self.tree.blockSignals(True)
                iterator = QtWidgets.QTreeWidgetItemIterator(self.tree)
                while iterator.value():
                    item = iterator.value()
                    long_name = item.data(0, QtCore.Qt.UserRole)
                    if long_name and long_name in prev_sel:
                        item.setSelected(True)
                    iterator += 1
                self.tree.blockSignals(False)
                self.tree._update_group_tints()
        finally:
            self._syncing = False

    # ── Right-click context menu ─────────────────────────────────────────

    def _show_context_menu(self, pos):
        """Build and show a context menu for custom group management."""
        item = self.tree.itemAt(pos)
        menu = QtWidgets.QMenu(self.tree)

        selected_leaves = [
            it for it in self.tree.selectedItems()
            if not it.data(0, QtCore.Qt.UserRole + 1)
        ]
        custom_groups = self._load_custom_groups()

        # --- Actions on a custom group header ---
        if item and item.data(0, QtCore.Qt.UserRole + 1) and item.data(0, self._CUSTOM_GROUP_ROLE):
            grp_name = item.text(0)
            menu.addAction("Rename Group\u2026", lambda: self._rename_group(grp_name))
            menu.addAction("Delete Group", lambda: self._delete_group(grp_name))
            menu.addSeparator()

        # --- New Group from selection ---
        if selected_leaves:
            menu.addAction("New Group\u2026", lambda: self._new_group_from_selection(selected_leaves))

            # --- Add to existing group submenu ---
            if custom_groups:
                sub = menu.addMenu("Add to Group")
                for name in sorted(custom_groups):
                    sub.addAction(name, lambda n=name: self._add_to_group(n, selected_leaves))

            # --- Remove from Group (if any selected items are in a custom group) ---
            in_custom = any(
                it.parent() and it.parent().data(0, self._CUSTOM_GROUP_ROLE)
                for it in selected_leaves
            )
            if in_custom:
                menu.addAction("Remove from Group", lambda: self._remove_from_group(selected_leaves))

        if menu.isEmpty():
            return
        menu.exec_(self.tree.viewport().mapToGlobal(pos))

    def _new_group_from_selection(self, selected_leaves):
        """Prompt for a name and create a new custom group with selected items."""
        name, ok = QtWidgets.QInputDialog.getText(
            self, "New Group", "Group name:"
        )
        if not ok or not name.strip():
            return
        name = name.strip()
        groups = self._load_custom_groups()
        if name in groups:
            QtWidgets.QMessageBox.warning(
                self, "Duplicate", f"A group named \"{name}\" already exists."
            )
            return
        long_names = []
        for it in selected_leaves:
            ln = it.data(0, QtCore.Qt.UserRole)
            if ln:
                long_names.append(ln)
        groups[name] = long_names
        self._save_custom_groups(groups)
        self._refresh()

    def _add_to_group(self, group_name, selected_leaves):
        """Add selected items to an existing custom group."""
        groups = self._load_custom_groups()
        existing = set(groups.get(group_name, []))
        for it in selected_leaves:
            ln = it.data(0, QtCore.Qt.UserRole)
            if ln:
                existing.add(ln)
        groups[group_name] = list(existing)
        self._save_custom_groups(groups)
        self._refresh()

    def _remove_from_group(self, selected_leaves):
        """Remove selected items from their custom groups."""
        groups = self._load_custom_groups()
        to_remove = set()
        for it in selected_leaves:
            ln = it.data(0, QtCore.Qt.UserRole)
            if ln:
                to_remove.add(ln)
        for name in list(groups):
            groups[name] = [n for n in groups[name] if n not in to_remove]
            if not groups[name]:
                del groups[name]
        self._save_custom_groups(groups)
        self._refresh()

    def _rename_group(self, old_name):
        """Rename a custom group."""
        new_name, ok = QtWidgets.QInputDialog.getText(
            self, "Rename Group", "New name:", text=old_name
        )
        if not ok or not new_name.strip() or new_name.strip() == old_name:
            return
        new_name = new_name.strip()
        groups = self._load_custom_groups()
        if new_name in groups:
            QtWidgets.QMessageBox.warning(
                self, "Duplicate", f"A group named \"{new_name}\" already exists."
            )
            return
        groups[new_name] = groups.pop(old_name, [])
        self._save_custom_groups(groups)
        self._refresh()

    def _delete_group(self, group_name):
        """Delete a custom group (items move back to Ungrouped)."""
        groups = self._load_custom_groups()
        groups.pop(group_name, None)
        self._save_custom_groups(groups)
        self._refresh()

    # ── Group click → select all children ─────────────────────────────────

    def _on_group_expanded(self, item):
        pass  # default behaviour is fine

    def _on_group_collapsed(self, item):
        pass

    # ── Selection sync: tree → viewport ───────────────────────────────────

    def _apply_maya_selection(self, long_names):
        """Sync a definitive list of long-names to Maya's selection."""
        if self._syncing:
            return
        self._syncing = True
        try:
            nodes = [n for n in long_names if n and cmds.objExists(n)]
            log.debug("_apply_maya_selection: selecting %d nodes", len(nodes))
            if nodes:
                cmds.select(nodes, replace=True)
            else:
                cmds.select(clear=True)
        finally:
            self._syncing = False

    # ── Selection sync: viewport → tree ───────────────────────────────────

    def _sync_from_viewport(self):
        """Highlight items in the tree that match Maya's current selection."""
        if self._syncing:
            return

        self._syncing = True
        try:
            sel = set(cmds.ls(selection=True, long=True) or [])
            log.debug("_sync_from_viewport: %d items from Maya", len(sel))
            self.tree.blockSignals(True)
            self.tree.clearSelection()

            iterator = QtWidgets.QTreeWidgetItemIterator(self.tree)
            while iterator.value():
                item = iterator.value()
                long_name = item.data(0, QtCore.Qt.UserRole)
                if long_name and long_name in sel:
                    item.setSelected(True)
                iterator += 1

            self.tree.blockSignals(False)
            self.tree._update_group_tints()
        finally:
            self._syncing = False

    def _on_viewport_selection_changed(self):
        """ScriptJob callback for SelectionChanged."""
        if self._syncing:
            return
        # Don't let Maya override tree selection while we own the interaction
        if self.tree._handled:
            log.debug("_on_viewport_selection_changed: suppressed (tree._handled)")
            return
        self._sync_from_viewport()

    # ── ScriptJobs ────────────────────────────────────────────────────────

    def _install_script_jobs(self):
        self._script_jobs = [
            cmds.scriptJob(
                event=["SelectionChanged", self._on_viewport_selection_changed],
                parent=self.TOOL_NAME,
            ),
            cmds.scriptJob(
                event=["DagObjectCreated", self._refresh],
                parent=self.TOOL_NAME,
            ),
            cmds.scriptJob(
                event=["NameChanged", self._refresh],
                parent=self.TOOL_NAME,
            ),
            cmds.scriptJob(
                event=["SceneOpened", self._refresh],
                parent=self.TOOL_NAME,
            ),
            cmds.scriptJob(
                event=["NewSceneOpened", self._refresh],
                parent=self.TOOL_NAME,
            ),
        ]

    # ── Cleanup ───────────────────────────────────────────────────────────

    def closeEvent(self, event):
        for job_id in self._script_jobs:
            try:
                if cmds.scriptJob(exists=job_id):
                    cmds.scriptJob(kill=job_id, force=True)
            except RuntimeError:
                pass
        self._script_jobs.clear()
        super().closeEvent(event)


# ── Module-level convenience launcher ─────────────────────────────────────

def show():
    SelectorTool.show()
