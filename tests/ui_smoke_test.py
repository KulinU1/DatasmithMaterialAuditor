# -*- coding: utf-8 -*-
"""Create and close the current source PySide window in a UE commandlet."""

import os
import sys
import unreal
from PySide6.QtCore import Qt


PLUGIN_PYTHON = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "Content", "Python")
)
if PLUGIN_PYTHON not in sys.path:
    sys.path.insert(0, PLUGIN_PYTHON)
for module_name in list(sys.modules):
    if module_name == "material_auditor" or module_name.startswith(
        "material_auditor."
    ):
        del sys.modules[module_name]

from material_auditor import ui, version


window = ui.show_window()
assert not hasattr(window, "_legacy_model_page")
assert not hasattr(window, "model_table")
assert not hasattr(window, "process_models")
assert not hasattr(ui, "model_processor")
if window is None:
    raise RuntimeError("show_window returned None")
if "Datasmith" not in window.windowTitle():
    raise RuntimeError("unexpected window title: {}".format(window.windowTitle()))
flags = window.windowFlags()
if not flags & Qt.WindowType.WindowMinimizeButtonHint:
    raise RuntimeError("window has no minimize button")
if flags & Qt.WindowType.WindowStaysOnTopHint:
    raise RuntimeError("window must not stay on top")
if window.windowType() != Qt.WindowType.Window:
    raise RuntimeError("window is not a standard Window: {}".format(window.windowType()))
if window.tabs.count() != 3:
    raise RuntimeError("expected material, UV, and mesh-quality tabs")
if version.PLUGIN_VERSION not in window.windowTitle():
    raise RuntimeError("window did not load the {} UI".format(version.PLUGIN_VERSION))
if not window.capture_scope_button.isEnabled():
    raise RuntimeError("scan-scope capture button must be enabled while idle")
if window.cancel_scan_button.isEnabled():
    raise RuntimeError("cancel button must be disabled while idle")
if window.uv_large_size.value() <= 0 or window.uv_small_size.value() <= 0:
    raise RuntimeError("UV projection sizes must be positive")
if hasattr(window, "model_backup_existing"):
    raise RuntimeError("direct overwrite must not expose a backup checkbox")
if "直接覆盖" not in window.uv_apply_button.text():
    raise RuntimeError("UV direct-overwrite button is missing")
if "Content Browser" not in window.locate_filtered_materials_button.text():
    raise RuntimeError("material locator button is missing")
if window.tabs.tabText(2) != "模型质量检查（测试中）":
    raise RuntimeError("mesh-quality tab title is missing")
if window.mesh_quality_table.columnCount() != 10:
    raise RuntimeError("mesh-quality result table columns are incomplete")
if window.mesh_quality_table.horizontalHeaderItem(4).text() != "组件实例数":
    raise RuntimeError("mesh-quality component count semantics are unclear")
if window.mesh_quality_table.horizontalHeaderItem(7).text() != "命中元素数":
    raise RuntimeError("mesh-quality affected element semantics are unclear")
if "结果组" not in window.mesh_quality_summary.toolTip():
    raise RuntimeError("mesh-quality summary count semantics are unclear")
if "只读" not in window.mesh_quality_scan_button.text():
    raise RuntimeError("mesh-quality scan must be clearly marked read-only")
if "红色线框" not in window.mesh_quality_preview_button.text():
    raise RuntimeError("error-region highlight button is missing")
if "Content Browser" not in window.mesh_quality_locate_asset_button.text():
    raise RuntimeError("mesh-quality asset locator is missing")
if window._mesh_quality_job_state is not None:
    raise RuntimeError("mesh-quality queue must be idle at startup")
window.close()
unreal.log("[MaterialAuditorUISmokeTest] PASS")
