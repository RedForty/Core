"""
ExampleTool
===========
Template for a new Maya workspace tool. Copy this file for each new tool.

Steps to make your own:
    1. Copy this file, e.g. my_new_tool.py
    2. Rename the class (e.g. MyNewTool)
    3. Set TOOL_NAME to a unique string (no spaces)
    4. Set TOOL_TITLE to whatever you want in the panel header
    5. Implement build_ui() — add widgets to self.main_layout
    6. Update the module-level show() to reference your class name

Launch from Script Editor:
    from my_tools.example_tool import ExampleTool
    ExampleTool.show()

Or via the module-level helper:
    import my_tools.example_tool
    my_tools.example_tool.show()
"""

try:
    from PySide2 import QtWidgets, QtCore
except ImportError:
    from PySide6 import QtWidgets, QtCore

from .base import WorkspaceToolBase


class ExampleTool(WorkspaceToolBase):

    TOOL_NAME      = "ExampleTool"    # Must be unique across all your tools
    TOOL_TITLE     = "Example Tool"   # Panel header label
    DEFAULT_WIDTH  = 320
    DEFAULT_HEIGHT = 480

    def build_ui(self):
        """
        Populate self.main_layout with your widgets.
        This is the only method you need to implement for a basic tool.
        """

        # ── Replace everything below with your own widgets ────────────────

        title_label = QtWidgets.QLabel("Example Tool")
        title_label.setAlignment(QtCore.Qt.AlignCenter)
        title_label.setStyleSheet("font-size: 14px; font-weight: bold; padding: 8px;")
        self.main_layout.addWidget(title_label)

        separator = QtWidgets.QFrame()
        separator.setFrameShape(QtWidgets.QFrame.HLine)
        separator.setFrameShadow(QtWidgets.QFrame.Sunken)
        self.main_layout.addWidget(separator)

        info_label = QtWidgets.QLabel(
            "This is a boilerplate tool.\n"
            "Replace build_ui() with your own widgets."
        )
        info_label.setAlignment(QtCore.Qt.AlignCenter)
        info_label.setWordWrap(True)
        self.main_layout.addWidget(info_label)

        self.main_layout.addSpacing(12)

        test_btn = QtWidgets.QPushButton("Test Button")
        test_btn.clicked.connect(self._on_test_clicked)
        self.main_layout.addWidget(test_btn)

        self.main_layout.addStretch()

        # Status bar at the bottom — handy for feedback
        self.status_label = QtWidgets.QLabel("Ready.")
        self.status_label.setStyleSheet("color: grey; font-size: 10px; padding: 2px;")
        self.main_layout.addWidget(self.status_label)

    # ── Slots / logic ─────────────────────────────────────────────────────────

    def _on_test_clicked(self):
        """Replace with your actual functionality."""
        print(f"[{self.TOOL_NAME}] Test button clicked.")
        self.status_label.setText("Button clicked.")


# ── Module-level convenience launcher ────────────────────────────────────────

def show():
    """
    Lets you launch the tool with a one-liner from the Script Editor:
        import my_tools.example_tool; my_tools.example_tool.show()
    """
    ExampleTool.show()
