# Core
Boilerplate base class for UIs in Maya

# Testing Guide — Maya Workspace Tool Boilerplate

These tests need to run inside a live Maya session.
Work through them top-to-bottom; later tests assume earlier ones passed.

---

## Setup (do this first)

Place `my_tools/` somewhere on disk, then add its parent to `sys.path`.
In the **Script Editor (Python tab)**:

```python
import sys
sys.path.insert(0, r"C:/path/to/parent/of/my_tools")  # adjust this path
```

---

## Test 1 — First Launch (floating, centered)

**Goal:** Tool opens floating, centered on Maya's main window. No saved state yet.

```python
# Clear any stale state first
from my_tools.example_tool import ExampleTool
ExampleTool.clear_saved_state()

# Open it
ExampleTool.show()
```

**Pass criteria:**
- [ ] Panel appears floating (not docked)
- [ ] Panel is visually centered over Maya's main window
- [ ] No errors in Script Editor output

---

## Test 2 — Singleton / Raise

**Goal:** Calling show() again raises the existing panel rather than creating a duplicate.

```python
# With the panel already open, run show() again
ExampleTool.show()
```

**Pass criteria:**
- [ ] No second panel appears
- [ ] If the panel was behind another window, it comes to front
- [ ] No errors in Script Editor output

---

## Test 3 — State Save on Close (floating)

**Goal:** Moving the panel and closing it saves position.

```python
import maya.cmds as cmds

# 1. With the panel open, drag it to a corner of your screen.
# 2. Close it with the X button.
# 3. Check that optionVars were written:
print("floating:", cmds.optionVar(q="ExampleTool_floating"))
print("x:",        cmds.optionVar(q="ExampleTool_x"))
print("y:",        cmds.optionVar(q="ExampleTool_y"))
print("width:",    cmds.optionVar(q="ExampleTool_width"))
print("height:",   cmds.optionVar(q="ExampleTool_height"))
```

**Pass criteria:**
- [ ] All five optionVars print non-zero values
- [ ] `floating` prints `1`
- [ ] x/y match roughly where you dragged the panel

---

## Test 4 — Position Restore (floating)

**Goal:** Reopening the tool puts it back where you left it.

```python
# Panel should be closed from Test 3.
ExampleTool.show()
```

**Pass criteria:**
- [ ] Panel reappears at (roughly) the same position as where you closed it
- [ ] No errors

---

## Test 5 — Dock State Save and Restore

**Goal:** Docking the panel, closing it, and reopening it restores the docked state.

```python
import maya.cmds as cmds

# 1. Open the panel.
ExampleTool.show()

# 2. Drag it to dock it (e.g., to the right side of Maya).
# 3. Close it with the X button.
# 4. Check state:
print("floating:", cmds.optionVar(q="ExampleTool_floating"))
print("dockArea:", cmds.optionVar(q="ExampleTool_dockArea"))

# 5. Reopen it:
ExampleTool.show()
```

**Pass criteria:**
- [ ] `floating` prints `0`
- [ ] `dockArea` prints the correct side ("left", "right", or "bottom")
- [ ] Reopening docks it to that same side (exact tab position may differ — known limitation)

---

## Test 6 — Startup Restore

**Goal:** If Maya closes with the panel open, it reopens on next launch.

**This test requires restarting Maya.**

```python
# Step 1: Make sure your path setup is in userSetup.py, e.g.:
#   import sys
#   sys.path.insert(0, r"C:/path/to/parent/of/my_tools")

# Step 2: Open the panel.
from my_tools.example_tool import ExampleTool
ExampleTool.show()

# Step 3: Move it somewhere recognizable on screen.
# Step 4: Close Maya normally (File > Quit).
# Step 5: Reopen Maya.
```

**Pass criteria:**
- [ ] Panel reappears automatically without any manual `show()` call
- [ ] Position matches where it was when Maya closed
- [ ] No errors in Script Editor / Output Window on startup

**If this fails:** Check that your `sys.path.insert` is in `userSetup.py`, not
just pasted into the Script Editor. The `uiScript` Maya stores only works if
the module is importable at startup time.

---

## Test 7 — Subclass Verification

**Goal:** Confirm the boilerplate works correctly for a second tool without any base.py changes.

Paste into Script Editor:

```python
from my_tools.base import WorkspaceToolBase
from PySide2 import QtWidgets

class QuickTestTool(WorkspaceToolBase):
    TOOL_NAME  = "QuickTestTool"
    TOOL_TITLE = "Quick Test"
    DEFAULT_WIDTH  = 200
    DEFAULT_HEIGHT = 150

    def build_ui(self):
        lbl = QtWidgets.QLabel("Subclass works.")
        self.main_layout.addWidget(lbl)

QuickTestTool.show()
```

**Pass criteria:**
- [ ] A second panel opens independently of ExampleTool
- [ ] The two panels are separate singletons (each has its own state)
- [ ] Closing one doesn't affect the other

---

## Known Limitations

- **Docked tab position:** When restoring a docked panel, we restore the *side*
  (left/right/bottom) but not the exact tab group. This is a Maya API limitation.

- **Startup restore requires sys.path:** The `uiScript` Maya saves is a Python
  import string. If your package isn't on `sys.path` at startup, restore silently
  fails (Maya suppresses uiScript errors by default).

- **Inline class definitions won't startup-restore:** Test 7's inline class
  can't restore on startup because `__module__` will be `__main__`. Move tools
  into proper .py files for real use.
