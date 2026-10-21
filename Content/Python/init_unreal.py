# -*- coding: utf-8 -*-
"""Unreal startup entry point for Datasmith Material Auditor."""

import unreal


OWNER = "DatasmithMaterialAuditorOwner"
MENU_NAME = "DatasmithMaterialAuditorMenu"
_retry_handle = None
_retry_count = 0


def register_menu():
    try:
        menus = unreal.ToolMenus.get()
        main_menu = menus.find_menu("LevelEditor.MainMenu")
        if not main_menu:
            return False

        if menus.is_menu_registered(MENU_NAME):
            return True

        submenu = main_menu.add_sub_menu(
            owner=OWNER,
            section_name="Tools",
            name=MENU_NAME,
            label="Datasmith 场景处理",
            tool_tip="Datasmith 材质审计、模型质量检查、缓冲区预览与一键盒体 UV",
        )

        entry = unreal.ToolMenuEntry(
            name="OpenDatasmithMaterialAuditor",
            type=unreal.MultiBlockType.MENU_ENTRY,
        )
        entry.set_label("打开 Datasmith 场景处理器")
        entry.set_tool_tip(
            "扫描选中的 Datasmith 场景，检查材质、模型法线和 UV，并执行可控处理。"
        )
        entry.set_string_command(
            unreal.ToolMenuStringCommandType.PYTHON,
            "Custom",
            "import importlib; import material_auditor.ui as material_auditor_ui; "
            "material_auditor_ui = importlib.reload(material_auditor_ui); "
            "material_auditor_ui.show_window()",
        )
        submenu.add_menu_entry("Items", entry)
        menus.refresh_all_widgets()
        unreal.log("[MaterialAuditor] 顶部菜单注册完成。")
        return True
    except Exception as exc:
        unreal.log_error("[MaterialAuditor] 菜单注册失败: {}".format(exc))
        return False


def _retry_register_menu(_delta_seconds):
    global _retry_handle, _retry_count
    _retry_count += 1
    if register_menu() or _retry_count >= 300:
        if _retry_handle is not None:
            unreal.unregister_slate_post_tick_callback(_retry_handle)
            _retry_handle = None
        if _retry_count >= 300:
            unreal.log_warning("[MaterialAuditor] 等待主菜单超时；可在 Python 控制台重新执行 init_unreal.py。")


if not register_menu():
    try:
        _retry_handle = unreal.register_slate_post_tick_callback(_retry_register_menu)
    except Exception:
        unreal.log_warning("[MaterialAuditor] LevelEditor.MainMenu 尚未就绪。")
