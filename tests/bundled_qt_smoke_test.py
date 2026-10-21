# -*- coding: utf-8 -*-
"""Run in an isolated UE5.8 project with the deployed plugin enabled."""

import os
from pathlib import Path
import sys

import unreal

plugin = Path(unreal.Paths.convert_relative_path_to_full(unreal.Paths.project_plugins_dir())) / "DatasmithMaterialAuditor"
bundle = (plugin / "Content/Python/Lib/Win64/site-packages").resolve()
assert bundle.is_dir(), "Bundled Qt directory missing"
assert any(Path(p).resolve() == bundle for p in sys.path if p), "UE did not register the bundled dependency path"
assert "PySide6" not in sys.modules and "shiboken6" not in sys.modules, "Qt was already loaded; test needs a fresh process"

# Simulate a colleague with no engine, user, or project-installed Qt packages.
sys.path[:] = [p for p in sys.path if "site-packages" not in p.lower() or Path(p).resolve() == bundle]
os.environ["QT_QPA_PLATFORM"] = "offscreen"

import PySide6
import shiboken6
from PySide6 import QtCore, QtGui, QtWidgets

for module in (PySide6, shiboken6, QtCore, QtGui, QtWidgets):
    assert Path(module.__file__).resolve().is_relative_to(bundle), module.__file__
assert PySide6.__version__ == shiboken6.__version__ == "6.11.1"
assert (bundle / "PySide6/plugins/platforms/qwindows.dll").is_file()
assert (bundle / "PySide6/Qt6Widgets.dll").is_file()
assert list(bundle.glob("*.dist-info/licenses/*")), "Package license files missing"

from material_auditor import ui

assert Path(ui.__file__).resolve().is_relative_to(plugin.resolve()), ui.__file__
window = ui.show_window()
assert window is not None and "Datasmith" in window.windowTitle()
assert window.tabs.count() == 3
QtWidgets.QApplication.processEvents()
window.close()
unreal.log("[BundledQtSmokeTest] PASS; PySide6={}; shiboken6={}; root={}".format(PySide6.__version__, shiboken6.__version__, bundle))
