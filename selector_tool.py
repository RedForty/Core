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
except ImportError:
    from PySide6 import QtWidgets, QtCore, QtGui

from .base import WorkspaceToolBase

log = logging.getLogger(__name__)
log.setLevel(logging.DEBUG)


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
                return

            # --- group header: select all children ---
            if item.data(0, QtCore.Qt.UserRole + 1):
                self._handled = True
                self._painting = False
                self.blockSignals(True)
                self.clearSelection()
                selected_keys = []
                for i in range(item.childCount()):
                    child = item.child(i)
                    child.setSelected(True)
                    key = self._item_key(child)
                    if key:
                        selected_keys.append(key)
                self.blockSignals(False)
                if self._sync_callback:
                    self._sync_callback(selected_keys)
                return

            # --- leaf item ---
            mods = event.modifiers()
            shift = mods & QtCore.Qt.ShiftModifier
            ctrl = mods & QtCore.Qt.ControlModifier

            self._handled = True
            self.setCurrentItem(item)
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
                self._pre_drag_selection = set()
                self._drag_deselecting = False
                log.debug("plain-click: new anchor=%s", item.text(0))
            else:
                # Ctrl+click — new anchor at clicked item, add/remove mode
                self._anchor_key = self._item_key(item)
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
                self.setCurrentItem(item)
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
        """Return the leaf item matching ``_anchor_key``, or *None*."""
        if not self._anchor_key:
            return None
        for item in leaves:
            if self._item_key(item) == self._anchor_key:
                return item
        return None

    def _apply_range(self, end_item):
        """Select (or deselect) the contiguous range from anchor to *end_item*."""
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
        range_set = set(leaves[lo:hi + 1])

        self.blockSignals(True)
        self.clearSelection()
        selected_keys = []
        if self._drag_deselecting:
            for item in leaves:
                should_select = (
                    item in self._pre_drag_selection and item not in range_set
                )
                item.setSelected(should_select)
                if should_select:
                    key = self._item_key(item)
                    if key:
                        selected_keys.append(key)
        else:
            for item in leaves:
                should_select = (
                    item in range_set or item in self._pre_drag_selection
                )
                item.setSelected(should_select)
                if should_select:
                    key = self._item_key(item)
                    if key:
                        selected_keys.append(key)
        self.blockSignals(False)

        if self._sync_callback:
            self._sync_callback(selected_keys)


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
        # --- type filter ---
        self.filter_edit = QtWidgets.QLineEdit()
        self.filter_edit.setPlaceholderText("Filter types  (e.g. joint | transform)")
        self.filter_edit.setClearButtonEnabled(True)
        self.filter_edit.returnPressed.connect(self._refresh)
        self.main_layout.addWidget(self.filter_edit)

        # debounce timer so typing doesn't hammer the scene query
        self._filter_timer = QtCore.QTimer(self)
        self._filter_timer.setSingleShot(True)
        self._filter_timer.setInterval(400)
        self._filter_timer.timeout.connect(self._refresh)
        self.filter_edit.textChanged.connect(self._restart_filter_timer)

        # --- tree ---
        self.tree = _PaintSelectTree()
        self.tree._sync_callback = self._apply_maya_selection
        self.tree.itemExpanded.connect(self._on_group_expanded)
        self.tree.itemCollapsed.connect(self._on_group_collapsed)
        self.main_layout.addWidget(self.tree)

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
        self._refresh()
        self._install_script_jobs()

    # ── Filter helpers ────────────────────────────────────────────────────

    def _restart_filter_timer(self):
        self._filter_timer.start()

    def _parsed_types(self):
        """Return a list of Maya node types from the filter field."""
        raw = self.filter_edit.text().strip()
        if not raw:
            return []
        return [t.strip() for t in raw.split("|") if t.strip()]

    # ── Scene query & tree build ──────────────────────────────────────────

    def _refresh(self):
        """Re-scan the scene and rebuild the tree."""
        self._filter_timer.stop()

        self._syncing = True
        try:
            self.tree.clear()

            types = self._parsed_types()
            if not types:
                self.status_label.setText("Enter a type to filter")
                return

            # Gather all matching nodes
            matching = set()
            for t in types:
                try:
                    matching.update(cmds.ls(type=t, long=True) or [])
                except RuntimeError:
                    pass  # invalid type name — skip

            if not matching:
                self.status_label.setText("0 items")
                return

            # Gather selection sets and their members
            all_sets = cmds.ls(type="objectSet") or []
            # Exclude Maya's default sets
            default_sets = {
                "defaultLightSet", "defaultObjectSet",
                "initialParticleSE", "initialShadingGroup",
            }
            user_sets = [
                s for s in all_sets
                if s not in default_sets
                and not cmds.objectType(s, isAType="shadingEngine")
            ]

            grouped = {}    # set_name -> [long_name, ...]
            assigned = set()

            for s in sorted(user_sets):
                members = cmds.sets(s, q=True, nodesOnly=True) or []
                long_members = []
                for m in members:
                    long_names = cmds.ls(m, long=True) or []
                    long_members.extend(long_names)
                group_nodes = [n for n in long_members if n in matching]
                if group_nodes:
                    grouped[s] = sorted(group_nodes, key=lambda n: n.rsplit("|", 1)[-1])
                    assigned.update(group_nodes)

            ungrouped = sorted(
                matching - assigned,
                key=lambda n: n.rsplit("|", 1)[-1],
            )

            item_count = 0

            # Build grouped sections
            for set_name, nodes in sorted(grouped.items()):
                group_item = QtWidgets.QTreeWidgetItem([set_name])
                group_item.setData(0, QtCore.Qt.UserRole + 1, True)  # is_group flag
                font = group_item.font(0)
                font.setBold(True)
                group_item.setFont(0, font)
                self.tree.addTopLevelItem(group_item)

                for long_name in nodes:
                    short = long_name.rsplit("|", 1)[-1]
                    child = QtWidgets.QTreeWidgetItem([short])
                    child.setData(0, QtCore.Qt.UserRole, long_name)  # store long name
                    child.setData(0, QtCore.Qt.UserRole + 1, False)
                    group_item.addChild(child)
                    item_count += 1

                group_item.setExpanded(True)

            # Build ungrouped section
            if ungrouped:
                ug_item = QtWidgets.QTreeWidgetItem([self.UNGROUPED_LABEL])
                ug_item.setData(0, QtCore.Qt.UserRole + 1, True)
                font = ug_item.font(0)
                font.setBold(True)
                ug_item.setFont(0, font)
                self.tree.addTopLevelItem(ug_item)

                for long_name in ungrouped:
                    short = long_name.rsplit("|", 1)[-1]
                    child = QtWidgets.QTreeWidgetItem([short])
                    child.setData(0, QtCore.Qt.UserRole, long_name)
                    child.setData(0, QtCore.Qt.UserRole + 1, False)
                    ug_item.addChild(child)
                    item_count += 1

                ug_item.setExpanded(True)

            self.status_label.setText(f"{item_count} items")

            # Restore selection highlight from Maya's current selection
            self._sync_from_viewport()
        finally:
            self._syncing = False

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
