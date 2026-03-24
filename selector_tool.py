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
from contextlib import contextmanager

import maya.cmds as cmds

try:
    from PySide2 import QtWidgets, QtCore, QtGui
    from shiboken2 import isValid
except ImportError:
    from PySide6 import QtWidgets, QtCore, QtGui
    from shiboken6 import isValid

from .base import WorkspaceToolBase


@contextmanager
def _signals_blocked(widget):
    """Block Qt signals for the duration of the context, even if an exception occurs."""
    widget.blockSignals(True)
    try:
        yield
    finally:
        widget.blockSignals(False)

log = logging.getLogger(__name__)
log.setLevel(logging.INFO)


# ---------------------------------------------------------------------------
#   Named data-roles for QTreeWidgetItem.data(0, role)
# ---------------------------------------------------------------------------
ROLE_LONG_NAME    = QtCore.Qt.UserRole       # str  – DAG long name
ROLE_IS_GROUP     = QtCore.Qt.UserRole + 1   # bool – True for group headers
ROLE_CUSTOM_GROUP = QtCore.Qt.UserRole + 2   # bool – True for custom groups
ROLE_TINT         = QtCore.Qt.UserRole + 3   # int  – tint level (see TINT_*)

TINT_NONE = 0
TINT_SOME = 1
TINT_ALL  = 2


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

    Tint levels are cached per group header and only recomputed when
    _update_group_tints() is called on the tree, not on every repaint.
    """

    COLOR_SOME = QtGui.QColor(70, 120, 180, 90)   # dim blue
    COLOR_ALL  = QtGui.QColor(90, 150, 220, 140)   # brighter blue

    def paint(self, painter, option, index):
        item = self.parent().itemFromIndex(index)
        if item and item.data(0, ROLE_IS_GROUP):
            tint = item.data(0, ROLE_TINT)
            if tint == TINT_ALL:
                color = self.COLOR_ALL
            elif tint == TINT_SOME:
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

    paintSelectStarted  = QtCore.Signal()
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
        self._key_to_items = {}  # {long_name: [items]} for O(1) lookup
        self.setSelectionMode(QtWidgets.QAbstractItemView.ExtendedSelection)
        self.setDragEnabled(False)
        self.setAcceptDrops(False)
        self.setRootIsDecorated(True)
        self.setHeaderHidden(True)
        self.setIndentation(16)
        self.setItemDelegate(_GroupTintDelegate(self))

    # -- public API (used by SelectorTool) ---------------------------------

    def set_sync_callback(self, callback):
        """Set the callback invoked when paint-select changes the selection."""
        self._sync_callback = callback

    def invalidate_anchor(self):
        """Clear the cached anchor item ref (call before rebuilding the tree)."""
        self._anchor_item = None

    def clear_key_index(self):
        """Clear the key→items lookup (call before rebuilding the tree)."""
        self._key_to_items.clear()

    def register_leaf(self, item):
        """Register a leaf item in the key→items lookup for O(1) sync."""
        key = self._item_key(item)
        if key:
            self._key_to_items.setdefault(key, []).append(item)

    @property
    def key_to_items(self):
        """Read-only access to the {long_name: [items]} index."""
        return self._key_to_items

    @property
    def is_handling_input(self):
        """True while the tree is processing its own mouse interaction."""
        return self._handled

    def update_group_tints(self):
        """Recompute cached tint levels for all group headers, then repaint."""
        self._recompute_tints()
        self.viewport().update()

    # -- events ------------------------------------------------------------

    def mousePressEvent(self, event):
        if event.button() == QtCore.Qt.LeftButton:
            item = self.itemAt(event.pos())

            # --- empty space: clear selection ---
            if not item:
                self._handled = True
                self._painting = False
                with _signals_blocked(self):
                    self.clearSelection()
                if self._sync_callback:
                    self._sync_callback([])
                self.update_group_tints()
                return

            # --- group header: select/toggle children ---
            if item.data(0, ROLE_IS_GROUP):
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

                with _signals_blocked(self):
                    # Collect keys for this group's children and current selection
                    leaves = self._leaf_items()
                    children = [item.child(i) for i in range(item.childCount())]
                    group_keys = {self._item_key(c) for c in children} - {None}
                    current_keys = {self._item_key(l) for l in leaves
                                    if l.isSelected()} - {None}

                    if ctrl:
                        # Ctrl+click group: toggle — remove keys if any
                        # children selected, otherwise add them
                        any_selected = bool(group_keys & current_keys)
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
                if self._sync_callback:
                    self._sync_callback(list(selected_keys_set))
                self.update_group_tints()
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
            self.paintSelectStarted.emit()
            self._apply_range(item)
            return
        self._handled = False
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if self._painting:
            item = self.itemAt(event.pos())
            if item and not item.data(0, ROLE_IS_GROUP):
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

    def _recompute_tints(self):
        """Recompute cached tint levels for all group headers."""
        for i in range(self.topLevelItemCount()):
            group = self.topLevelItem(i)
            if not group.data(0, ROLE_IS_GROUP):
                continue
            child_count = group.childCount()
            if not child_count:
                group.setData(0, ROLE_TINT, TINT_NONE)
                continue
            selected = sum(
                1 for c in range(child_count) if group.child(c).isSelected()
            )
            if selected == child_count:
                group.setData(0, ROLE_TINT, TINT_ALL)
            elif selected > 0:
                group.setData(0, ROLE_TINT, TINT_SOME)
            else:
                group.setData(0, ROLE_TINT, TINT_NONE)

    @staticmethod
    def _item_key(item):
        """Return the long name stored on *item* (used as a stable identity)."""
        return item.data(0, ROLE_LONG_NAME)

    def _leaf_items(self):
        """Return all visible non-group items in visual order."""
        items = []
        iterator = QtWidgets.QTreeWidgetItemIterator(
            self, QtWidgets.QTreeWidgetItemIterator.NoChildren
        )
        while iterator.value():
            item = iterator.value()
            if not item.data(0, ROLE_IS_GROUP):
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
        # Anchor's object no longer exists in the tree — reset so the next
        # click establishes a fresh anchor instead of silently failing.
        log.warning("Anchor item %r no longer in tree; resetting anchor.",
                    self._anchor_key)
        self._anchor_key = None
        self._anchor_item = None
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

        if self._drag_deselecting:
            selected_keys_set = pre_drag_keys - range_keys
        else:
            selected_keys_set = range_keys | pre_drag_keys

        with _signals_blocked(self):
            self.clearSelection()
            # Second pass: apply selection, including clones that share a key
            for item in leaves:
                key = self._item_key(item)
                item.setSelected(bool(key and key in selected_keys_set))
        # Set focus rect without changing selection state
        idx = self.indexFromItem(end_item)
        self.selectionModel().setCurrentIndex(
            idx, QtCore.QItemSelectionModel.NoUpdate
        )

        if self._sync_callback:
            self._sync_callback(list(selected_keys_set))
        self.update_group_tints()


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
        self._cached_collapsed = set()  # in-memory collapsed state
        super().__init__(parent)

    # ── UI ────────────────────────────────────────────────────────────────

    def build_ui(self):
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
        self.tree.set_sync_callback(self._apply_maya_selection)
        self.tree.paintSelectStarted.connect(self._open_undo_chunk)
        self.tree.paintSelectFinished.connect(self._close_undo_chunk)
        self.tree.itemExpanded.connect(self._on_group_expanded)
        self.tree.itemCollapsed.connect(self._on_group_collapsed)
        self.tree.setContextMenuPolicy(QtCore.Qt.CustomContextMenu)
        self.tree.customContextMenuRequested.connect(self._show_context_menu)
        self.main_layout.addWidget(self.tree)

        # --- status bar ---
        _status_style = "color: grey; font-size: 10px; padding: 2px;"
        status_layout = QtWidgets.QHBoxLayout()
        status_layout.setContentsMargins(0, 0, 0, 0)
        self.status_label = QtWidgets.QLabel("0 items")
        self.status_label.setStyleSheet(_status_style)
        self.sel_count_label = QtWidgets.QLabel("")
        self.sel_count_label.setStyleSheet(_status_style)
        self.sel_count_label.setAlignment(QtCore.Qt.AlignRight)
        status_layout.addWidget(self.status_label)
        status_layout.addWidget(self.sel_count_label)
        self.main_layout.addLayout(status_layout)

        # --- style tweaks for tree group headers ---
        self.tree.setStyleSheet("""
            QTreeWidget::item { padding: 1px 0px; }
        """)

        # Debounce timer for collapsed-state writes (avoids disk I/O per click)
        self._collapse_save_timer = QtCore.QTimer(self)
        self._collapse_save_timer.setSingleShot(True)
        self._collapse_save_timer.setInterval(500)
        self._collapse_save_timer.timeout.connect(self._flush_collapsed_groups)

        # Defer scene queries and scriptJobs until the workspaceControl
        # is fully initialised (avoids errors during __init__).
        cmds.evalDeferred(self._deferred_init)

    def _deferred_init(self):
        """Called via evalDeferred so the workspaceControl is fully ready."""
        if not isValid(self):
            return
        self._refresh()
        self._install_script_jobs()

    # ── Filter helpers ────────────────────────────────────────────────────

    def _restart_filter_timer(self):
        self._filter_timer.start()

    _all_node_types_cache = None  # class-level cache; stable within a session

    @classmethod
    def _all_node_types(cls):
        """Return the cached list of all Maya node types."""
        if cls._all_node_types_cache is None:
            cls._all_node_types_cache = cmds.allNodeTypes() or []
        return cls._all_node_types_cache

    @classmethod
    def _resolve_types(cls, token):
        """Resolve a user token to a list of Maya node types.

        Exact matches are returned directly.  If no exact match exists,
        all registered node types whose name contains *token* (case-
        insensitive) are returned — e.g. ``cam`` → ``[camera, ...]``.
        """
        all_types = cls._all_node_types()
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

    # Config keys — structured format keeps groups and metadata separate.
    _CFG_GROUPS    = "groups"
    _CFG_COLLAPSED = "collapsed"

    # Legacy sentinel used by the old flat config format.
    _LEGACY_COLLAPSED_KEY = "__collapsed__"

    @classmethod
    def _migrate_scene_config(cls, cfg):
        """Migrate old flat config ``{groupName: [...], __collapsed__: [...]}``
        to the structured format ``{groups: {...}, collapsed: [...]}``.

        Returns the config unchanged if it is already in the new format or empty.
        """
        if not cfg or cls._CFG_GROUPS in cfg:
            return cfg  # already new format (or empty)
        # Old format detected — every key except __collapsed__ is a group.
        collapsed = cfg.pop(cls._LEGACY_COLLAPSED_KEY, [])
        return {
            cls._CFG_GROUPS:    cfg,   # remaining keys are groups
            cls._CFG_COLLAPSED: collapsed,
        }

    def _load_collapsed_groups(self):
        """Load collapsed state from disk into the in-memory cache and return it."""
        cfg = self._migrate_scene_config(self._load_scene_config())
        self._cached_collapsed = set(cfg.get(self._CFG_COLLAPSED, []))
        return self._cached_collapsed

    def _flush_collapsed_groups(self):
        """Write the in-memory collapsed set to disk (called by debounce timer)."""
        cfg = self._migrate_scene_config(self._load_scene_config())
        if self._cached_collapsed:
            cfg[self._CFG_COLLAPSED] = sorted(self._cached_collapsed)
        else:
            cfg.pop(self._CFG_COLLAPSED, None)
        self._save_scene_config(cfg)

    def _load_custom_groups(self):
        """Return custom groups for the current scene. {name: [long_names]}"""
        cfg = self._migrate_scene_config(self._load_scene_config())
        return dict(cfg.get(self._CFG_GROUPS, {}))

    def _save_custom_groups(self, groups):
        """Save custom groups for the current scene."""
        cfg = self._migrate_scene_config(self._load_scene_config())
        cfg[self._CFG_GROUPS] = dict(groups)
        self._save_scene_config(cfg)

    _RESERVED_GROUP_NAMES = frozenset({
        _CFG_GROUPS, _CFG_COLLAPSED, "__collapsed__",
    })

    @classmethod
    def _validate_group_name(cls, name):
        """Return an error message if *name* is invalid, or None if it is OK."""
        if not name or not name.strip():
            return "Group name cannot be empty."
        if name.startswith("__") and name.endswith("__"):
            return f'Names like "__{name[2:-2]}__" are reserved.'
        if name in cls._RESERVED_GROUP_NAMES:
            return f'"{name}" is a reserved name.'
        return None

    # ── Tree-item factories ─────────────────────────────────────────────

    _CUSTOM_GROUP_ROLE = ROLE_CUSTOM_GROUP

    @staticmethod
    def _make_leaf_item(long_name):
        """Create a leaf QTreeWidgetItem for a scene node."""
        short = long_name.rsplit("|", 1)[-1]
        item = QtWidgets.QTreeWidgetItem([short])
        item.setData(0, ROLE_LONG_NAME, long_name)
        item.setData(0, ROLE_IS_GROUP, False)
        return item

    @classmethod
    def _make_group_header(cls, label, is_custom=False):
        """Create a bold group-header QTreeWidgetItem."""
        item = QtWidgets.QTreeWidgetItem([label])
        item.setData(0, ROLE_IS_GROUP, True)
        item.setData(0, cls._CUSTOM_GROUP_ROLE, is_custom)
        font = item.font(0)
        font.setBold(True)
        item.setFont(0, font)
        return item

    # ── Scene query & tree build ──────────────────────────────────────────

    def _query_matching_nodes(self):
        """Query the scene and return the set of long-names that pass the filter.

        Returns ``None`` if the filter is empty (caller should show a placeholder).
        """
        include, exclude, show_shapes = self._parsed_types()
        if not include:
            return None

        raw_matches = set()
        for t in include:
            try:
                raw_matches.update(cmds.ls(type=t, long=True) or [])
            except RuntimeError:
                pass  # invalid type name — skip
        for t in exclude:
            try:
                raw_matches -= set(cmds.ls(type=t, long=True) or [])
            except RuntimeError:
                pass

        # Resolve shape nodes to their transform parents.
        matching = set()
        for node in raw_matches:
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
        return matching

    def _populate_tree(self, matching):
        """Build tree groups from *matching* long-names.  Returns item count."""
        custom_groups = self._load_custom_groups()
        collapsed = self._cached_collapsed
        assigned = set()
        item_count = 0

        # Custom groups (take priority)
        for grp_name in sorted(custom_groups):
            nodes = [n for n in custom_groups[grp_name] if n in matching]
            if not nodes:
                continue
            nodes.sort(key=lambda n: n.rsplit("|", 1)[-1])
            assigned.update(nodes)
            group_item = self._make_group_header(grp_name, is_custom=True)
            self.tree.addTopLevelItem(group_item)
            for long_name in nodes:
                leaf = self._make_leaf_item(long_name)
                group_item.addChild(leaf)
                self.tree.register_leaf(leaf)
                item_count += 1
            group_item.setExpanded(grp_name not in collapsed)

        # Selection-set groups (for remaining items)
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
                leaf = self._make_leaf_item(long_name)
                group_item.addChild(leaf)
                self.tree.register_leaf(leaf)
                item_count += 1
            group_item.setExpanded(s not in collapsed)

        # Ungrouped section
        ungrouped = sorted(
            matching - assigned,
            key=lambda n: n.rsplit("|", 1)[-1],
        )
        if ungrouped:
            ug_item = self._make_group_header(self.UNGROUPED_LABEL, is_custom=False)
            self.tree.addTopLevelItem(ug_item)
            for long_name in ungrouped:
                leaf = self._make_leaf_item(long_name)
                ug_item.addChild(leaf)
                self.tree.register_leaf(leaf)
                item_count += 1
            ug_item.setExpanded(self.UNGROUPED_LABEL not in collapsed)

        return item_count

    def _restore_tree_selection(self, prev_sel):
        """Re-select tree items whose long-names are in *prev_sel*."""
        if not prev_sel:
            return
        with _signals_blocked(self.tree):
            for long_name, items in self.tree.key_to_items.items():
                if long_name in prev_sel:
                    for item in items:
                        item.setSelected(True)
        self.tree.update_group_tints()
        self._update_sel_count()

    def _refresh(self):
        """Re-scan the scene and rebuild the tree."""
        if not isValid(self):
            return
        self._filter_timer.stop()

        prev_sel = set(cmds.ls(selection=True, long=True) or [])

        self._syncing = True
        try:
            self.tree.invalidate_anchor()
            self.tree.clear_key_index()
            with _signals_blocked(self.tree):
                self.tree.clear()

            matching = self._query_matching_nodes()
            if matching is None:
                self.status_label.setText("Enter a type to filter")
                return
            if not matching:
                self.status_label.setText("0 items")
                return

            self._load_collapsed_groups()  # refresh in-memory cache from disk
            item_count = self._populate_tree(matching)
            self.status_label.setText(f"{item_count} items")
            self._restore_tree_selection(prev_sel)
        finally:
            self._syncing = False

    # ── Right-click context menu ─────────────────────────────────────────

    def _show_context_menu(self, pos):
        """Build and show a context menu for custom group management."""
        item = self.tree.itemAt(pos)
        menu = QtWidgets.QMenu(self.tree)

        selected_leaves = [
            it for it in self.tree.selectedItems()
            if not it.data(0, ROLE_IS_GROUP)
        ]
        custom_groups = self._load_custom_groups()

        # --- Actions on a custom group header ---
        if item and item.data(0, ROLE_IS_GROUP) and item.data(0, self._CUSTOM_GROUP_ROLE):
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
        error = self._validate_group_name(name)
        if error:
            QtWidgets.QMessageBox.warning(self, "Invalid Name", error)
            return
        groups = self._load_custom_groups()
        if name in groups:
            QtWidgets.QMessageBox.warning(
                self, "Duplicate", f"A group named \"{name}\" already exists."
            )
            return
        long_names = []
        for it in selected_leaves:
            ln = it.data(0, ROLE_LONG_NAME)
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
            ln = it.data(0, ROLE_LONG_NAME)
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
            ln = it.data(0, ROLE_LONG_NAME)
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
        error = self._validate_group_name(new_name)
        if error:
            QtWidgets.QMessageBox.warning(self, "Invalid Name", error)
            return
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
        if item.data(0, ROLE_IS_GROUP):
            self._cached_collapsed.discard(item.text(0))
            self._collapse_save_timer.start()

    def _on_group_collapsed(self, item):
        if item.data(0, ROLE_IS_GROUP):
            self._cached_collapsed.add(item.text(0))
            self._collapse_save_timer.start()

    # ── Undo chunk for paint-select ──────────────────────────────────────

    _UNDO_CHUNK_NAME = "SelectorTool_paintSelect"

    def _open_undo_chunk(self):
        """Open a Maya undo chunk so the entire paint-drag is one undo step."""
        cmds.undoInfo(openChunk=True, chunkName=self._UNDO_CHUNK_NAME)

    def _close_undo_chunk(self):
        """Close the paint-select undo chunk."""
        cmds.undoInfo(closeChunk=True)

    # ── Selection sync: tree → viewport ───────────────────────────────────

    def _update_sel_count(self):
        """Update the selected-count label from the tree's current selection."""
        n = sum(
            1 for key, items in self.tree.key_to_items.items()
            if any(it.isSelected() for it in items)
        )
        self.sel_count_label.setText(f"{n} selected" if n else "")

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
            self._update_sel_count()
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
            with _signals_blocked(self.tree):
                self.tree.clearSelection()
                for long_name, items in self.tree.key_to_items.items():
                    if long_name in sel:
                        for item in items:
                            item.setSelected(True)
            self.tree.update_group_tints()
            self._update_sel_count()
        finally:
            self._syncing = False

    def _on_viewport_selection_changed(self):
        """ScriptJob callback for SelectionChanged."""
        if self._syncing:
            return
        # Don't let Maya override tree selection while we own the interaction
        if self.tree.is_handling_input:
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
        # Flush any pending debounced config writes
        if self._collapse_save_timer.isActive():
            self._collapse_save_timer.stop()
            self._flush_collapsed_groups()
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
