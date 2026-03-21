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

import maya.cmds as cmds

try:
    from PySide2 import QtWidgets, QtCore, QtGui
except ImportError:
    from PySide6 import QtWidgets, QtCore, QtGui

from .base import WorkspaceToolBase


# ---------------------------------------------------------------------------
#   Custom tree widget with paint-select (click-drag) support
# ---------------------------------------------------------------------------

class _PaintSelectTree(QtWidgets.QTreeWidget):
    """
    QTreeWidget subclass that lets the user click-and-drag across items
    to 'paint' a selection, similar to dragging over cells in a spreadsheet.
    """

    paintSelectFinished = QtCore.Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self._painting = False
        self._paint_modifier = None  # None = replace, Ctrl = toggle
        self._painted_items = set()
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
            if item and not item.data(0, QtCore.Qt.UserRole + 1):
                # Clicked on a non-group item — start painting
                self._painting = True
                self._painted_items.clear()
                ctrl = event.modifiers() & QtCore.Qt.ControlModifier
                self._paint_modifier = "ctrl" if ctrl else None
                if self._paint_modifier != "ctrl":
                    self.clearSelection()
                self._toggle_paint(item)
                return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if self._painting:
            item = self.itemAt(event.pos())
            if item and not item.data(0, QtCore.Qt.UserRole + 1):
                self._toggle_paint(item)
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        if self._painting and event.button() == QtCore.Qt.LeftButton:
            self._painting = False
            self._painted_items.clear()
            self.paintSelectFinished.emit()
            return
        super().mouseReleaseEvent(event)

    # -- helpers -----------------------------------------------------------

    def _toggle_paint(self, item):
        if item in self._painted_items:
            return
        self._painted_items.add(item)
        item.setSelected(True)


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
        self.tree.itemSelectionChanged.connect(self._on_tree_selection_changed)
        self.tree.itemExpanded.connect(self._on_group_expanded)
        self.tree.itemCollapsed.connect(self._on_group_collapsed)
        self.tree.paintSelectFinished.connect(self._on_tree_selection_changed)
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

        # initial populate & hooks
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

    def _on_tree_selection_changed(self):
        if self._syncing:
            return

        self._syncing = True
        try:
            nodes_to_select = []
            for item in self.tree.selectedItems():
                is_group = item.data(0, QtCore.Qt.UserRole + 1)
                if is_group:
                    # Select all children
                    for i in range(item.childCount()):
                        child = item.child(i)
                        child.setSelected(True)
                        long_name = child.data(0, QtCore.Qt.UserRole)
                        if long_name and cmds.objExists(long_name):
                            nodes_to_select.append(long_name)
                else:
                    long_name = item.data(0, QtCore.Qt.UserRole)
                    if long_name and cmds.objExists(long_name):
                        nodes_to_select.append(long_name)

            if nodes_to_select:
                cmds.select(nodes_to_select, replace=True)
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
