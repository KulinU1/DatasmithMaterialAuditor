# -*- coding: utf-8 -*-
"""Non-modal PySide6 window for the current Datasmith Material Auditor."""

from __future__ import annotations

import os
import sys
import traceback
import importlib
import math
from dataclasses import replace

import unreal
from PySide6.QtCore import Qt
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QApplication,
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QTextEdit,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from . import core, mesh_quality, version

if getattr(core, "API_VERSION", "") != version.CORE_API_VERSION:
    core = importlib.reload(core)
if getattr(mesh_quality, "API_VERSION", "") != version.MESH_QUALITY_API_VERSION:
    mesh_quality = importlib.reload(mesh_quality)

_WINDOW = globals().get("_WINDOW", None)
_QT_APP = globals().get("_QT_APP", None)

class _TreeHeaderProxy:
    """Expose one QTreeWidget header section like a QTableWidget header item."""

    def __init__(self, item, column):
        self._item = item
        self._column = int(column)

    def text(self):
        return self._item.text(self._column)

    def setToolTip(self, value):
        self._item.setToolTip(self._column, value)

    def toolTip(self):
        return self._item.toolTip(self._column)

class _MeshQualityTreeWidget(QTreeWidget):
    """Actor-grouped result tree with the old table header compatibility API."""

    def setHorizontalHeaderLabels(self, labels):
        self.setHeaderLabels(labels)

    def horizontalHeader(self):
        return self.header()

    def horizontalHeaderItem(self, column):
        # Older smoke tests and a few local helper scripts treated the result
        # view as a QTableWidget.  Keep this small compatibility surface while
        # the UI itself uses a hierarchical QTreeWidget.
        column = int(column)
        header = self.headerItem()
        if header and 0 <= column < self.columnCount():
            return _TreeHeaderProxy(header, column)
        return None

_MESH_QUALITY_ITEM_KIND_ROLE = int(Qt.ItemDataRole.UserRole)
_MESH_QUALITY_RECORD_ROLE = _MESH_QUALITY_ITEM_KIND_ROLE + 1
_MESH_QUALITY_CATEGORY_SUMMARY = (
    ("normal", "法线"),
    ("uv", "UV"),
    ("geometry", "几何"),
    ("transform", "变换"),
    ("system", "系统"),
)

def _default_rules_file() -> str:
    # Kept for compatibility with the scanner signature. Classification no
    # longer uses names or keywords.
    return os.path.join(os.path.dirname(__file__), "default_rules.json")

def _yes_no(value: bool) -> str:
    return "写入" if value else "不写入"

def _classification_label(value: str) -> str:
    return {
        "glass": "玻璃",
        "non_glass": "非玻璃",
        "issue": "渲染异常",
        "excluded": "特殊材质域",
    }.get(value, value)

_MESH_QUALITY_SEVERITY_LABELS = {
    "error": "错误",
    "warning": "警告",
    "info": "提示",
}

_MESH_QUALITY_ISSUE_LABELS = {
    "INVALID_NORMAL": "无效法线",
    "DEGENERATE_TRIANGLE": "退化三角面",
    "LOCAL_WINDING_CONFLICT": "局部绕序异常区域",
    "VERTEX_NORMAL_REVERSED": "顶点法线反向区域",
    "CLOSED_SHELL_INWARD": "闭合壳体整体朝内",
    "NON_MANIFOLD_TOPOLOGY": "非流形拓扑区域",
    "OPEN_MESH_DIRECTION_UNKNOWN": "开放网格方向未知",
    "ACTOR_NEGATIVE_SCALE": "Actor 镜像负缩放",
    "DEGENERATE_GEOMETRY": "退化三角形",
    "INVALID_VERTEX_NORMAL": "构建后无效法线",
    "NON_UNIT_VERTEX_NORMAL": "法线长度异常",
    "REVERSED_VERTEX_NORMAL": "顶点法线反向",
    "AMBIGUOUS_VERTEX_NORMAL": "顶点法线接近垂直",
    "WINDING_CONFLICT": "拓扑绕序冲突",
    "NON_MANIFOLD_EDGE": "非流形边",
    "MISSING_UV_CHANNEL": "缺少 UV 通道",
    "INVALID_TRIANGLE_UV": "无效 UV 坐标",
    "DEGENERATE_TRIANGLE_UV": "UV 压扁/退化",
    "UV_STRETCH": "UV 单向拉伸",
    "NATIVE_ANALYSIS_FAILED": "网格分析失败",
}

class MaterialAuditorWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Datasmith 场景处理器 {}".format(version.PLUGIN_VERSION))
        self.resize(1320, 780)
        self.setWindowModality(Qt.WindowModality.NonModal)
        # Behave like a normal desktop window: minimize/maximize/close buttons,
        # no always-on-top behavior, and normal z-order when focus moves to UE.
        self.setWindowFlags(
            Qt.WindowType.Window
            | Qt.WindowType.WindowTitleHint
            | Qt.WindowType.WindowSystemMenuHint
            | Qt.WindowType.WindowMinimizeButtonHint
            | Qt.WindowType.WindowMaximizeButtonHint
            | Qt.WindowType.WindowCloseButtonHint
        )
        self.setAttribute(Qt.WidgetAttribute.WA_QuitOnClose, False)
        self.records = []
        self.material_rows = []
        self.uv_records = []
        self.mesh_quality_rows = []
        self.scan_root_paths = []
        self.scan_root_labels = []
        self._scan_cancel_requested = False
        self._scan_active = False
        self._mesh_quality_job_state = None
        self._mesh_quality_tick_handle = None
        self._mesh_quality_preview_state = None
        self._mesh_quality_preview_handle = None
        self._mesh_quality_scanned_groups = 0
        self._mesh_quality_scan_summary = {}
        self._closing = False
        self._build_ui()
        self._load_state()

    def _build_ui(self):
        root = QWidget()
        self.setCentralWidget(root)
        root_layout = QVBoxLayout(root)

        scope_group = QGroupBox(
            "材质扫描范围（UV / 模型质量扫描直接使用当前选择）"
        )
        scope_layout = QHBoxLayout(scope_group)
        self.scan_scope_label = QLabel(
            "尚未锁定：请在 World Outliner 选择最上层 Datasmith Root。"
        )
        self.scan_scope_label.setWordWrap(True)
        self.scan_scope_label.setStyleSheet("color: #b8c7dc;")
        scope_layout.addWidget(self.scan_scope_label, 1)
        self.capture_scope_button = QPushButton("读取并锁定当前选中 Root")
        self.capture_scope_button.clicked.connect(self._capture_scan_scope)
        scope_layout.addWidget(self.capture_scope_button)
        self.cancel_scan_button = QPushButton("取消当前任务")
        self.cancel_scan_button.setEnabled(False)
        self.cancel_scan_button.clicked.connect(self._request_scan_cancel)
        scope_layout.addWidget(self.cancel_scan_button)
        self.scan_progress_label = QLabel("空闲")
        self.scan_progress_label.setMinimumWidth(145)
        scope_layout.addWidget(self.scan_progress_label)
        root_layout.addWidget(scope_group)

        self.tabs = QTabWidget()
        material_page = QWidget()
        layout = QVBoxLayout(material_page)

        intro = QLabel(
            "自动判断规则：不写入 World Normal 且不写入 Scene Depth 的表面视为玻璃；"
            "两个缓冲都写入的视为非玻璃；只写入其中一个的材质列为渲染异常并跳过。"
        )
        intro.setWordWrap(True)
        layout.addWidget(intro)

        scope = QLabel("处理范围：始终使用窗口上方已锁定的 Root；场景筛选选择不会覆盖它。")
        scope.setStyleSheet("color: #b8c7dc;")
        layout.addWidget(scope)

        settings = QGroupBox("处理设置")
        settings_layout = QVBoxLayout(settings)

        glass_row = QHBoxLayout()
        glass_row.addWidget(QLabel("替换用玻璃材质"))
        self.glass_path = QLineEdit()
        self.glass_path.setPlaceholderText("只需选择一次，工具会自动记住")
        self.glass_path.editingFinished.connect(self._save_state)
        glass_row.addWidget(self.glass_path, 1)
        pick_glass = QPushButton("读取 Content Browser 选中材质")
        pick_glass.clicked.connect(lambda: self._pick_selected_asset(self.glass_path))
        glass_row.addWidget(pick_glass)
        settings_layout.addLayout(glass_row)

        post_depth_row = QHBoxLayout()
        post_depth_row.addWidget(QLabel("场景深度后期材质"))
        self.post_depth_path = QLineEdit()
        self.post_depth_path.setPlaceholderText("Post Process 域材质或材质实例")
        self.post_depth_path.editingFinished.connect(self._save_state)
        post_depth_row.addWidget(self.post_depth_path, 1)
        pick_post_depth = QPushButton("读取 Content Browser 选中材质")
        pick_post_depth.clicked.connect(
            lambda: self._pick_selected_asset(self.post_depth_path)
        )
        post_depth_row.addWidget(pick_post_depth)
        settings_layout.addLayout(post_depth_row)

        post_depth_help = QLabel(
            "深度检查保持 Lit，临时创建不保存进关卡的无限范围 Post Process Volume，"
            "其中只加入上述材质；默认使用项目里的 M_PostDepth_10。"
        )
        post_depth_help.setWordWrap(True)
        post_depth_help.setStyleSheet("color: #9fb0c5;")
        settings_layout.addWidget(post_depth_help)

        normal_function_row = QHBoxLayout()
        normal_function_row.addWidget(QLabel("Normal 补全函数"))
        self.normal_function_path = QLineEdit()
        self.normal_function_path.setPlaceholderText(
            "在 Content Browser 选择你写好的 Material Function"
        )
        self.normal_function_path.editingFinished.connect(self._save_state)
        normal_function_row.addWidget(self.normal_function_path, 1)
        pick_normal_function = QPushButton("读取 Content Browser 选中函数")
        pick_normal_function.clicked.connect(self._pick_selected_function)
        normal_function_row.addWidget(pick_normal_function)
        settings_layout.addLayout(normal_function_row)

        self.auto_two_sided = QCheckBox("正式处理时，按唯一母材质去重并开启 Two Sided")
        self.auto_two_sided.setChecked(True)
        self.auto_two_sided.stateChanged.connect(self._save_state)
        settings_layout.addWidget(self.auto_two_sided)
        layout.addWidget(settings)

        action_row = QHBoxLayout()
        scan_button = QPushButton("1. 自动扫描（World Normal + Scene Depth 规则）")
        scan_button.clicked.connect(self.scan)
        action_row.addWidget(scan_button)
        apply_button = QPushButton("2. 一键替换玻璃并标准化")
        apply_button.clicked.connect(self.apply_processing)
        action_row.addWidget(apply_button)
        layout.addLayout(action_row)

        repair_row = QHBoxLayout()
        two_sided_button = QPushButton("仅为未开启双面的母材质批量开启双面")
        two_sided_button.clicked.connect(self.enable_two_sided)
        repair_row.addWidget(two_sided_button)
        normal_function_button = QPushButton(
            "仅为 Normal 未连接的母材质接入所选函数"
        )
        normal_function_button.clicked.connect(self.connect_normal_function)
        repair_row.addWidget(normal_function_button)
        layout.addLayout(repair_row)

        self.summary = QLabel("尚未扫描")
        self.summary.setStyleSheet("font-weight: bold; color: #d0d0d0;")
        layout.addWidget(self.summary)

        filter_row = QHBoxLayout()
        filter_row.addWidget(QLabel("显示筛选"))
        self.filter_combo = QComboBox()
        self.filter_combo.addItem("全部物品", "all")
        self.filter_combo.addItem("仅玻璃物品", "glass")
        self.filter_combo.addItem("仅非玻璃物品", "non_glass")
        self.filter_combo.addItem("仅渲染异常", "issue")
        self.filter_combo.addItem("仅特殊材质域", "excluded")
        self.filter_combo.addItem("仅 Normal 未连接", "normal_missing")
        self.filter_combo.currentIndexChanged.connect(self._apply_filter)
        filter_row.addWidget(self.filter_combo)
        select_filtered = QPushButton("在场景中选中当前筛选物品")
        select_filtered.clicked.connect(self.select_filtered_actors)
        filter_row.addWidget(select_filtered)
        self.locate_filtered_materials_button = QPushButton(
            "在 Content Browser 定位筛选材质"
        )
        self.locate_filtered_materials_button.setToolTip(
            "表格有选中行时只定位选中材质；否则定位当前显示筛选下的全部唯一材质。"
        )
        self.locate_filtered_materials_button.clicked.connect(
            self.locate_filtered_materials
        )
        filter_row.addWidget(self.locate_filtered_materials_button)
        filter_row.addStretch(1)
        layout.addLayout(filter_row)

        self.table = QTableWidget(0, 11)
        self.table.setHorizontalHeaderLabels(
            [
                "使用物品",
                "使用槽数",
                "当前材质",
                "Blend",
                "Shading",
                "World Normal",
                "Scene Depth",
                "Normal 输入",
                "母材质双面",
                "自动判定",
                "判定依据",
            ]
        )
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        header.setSectionResizeMode(10, QHeaderView.ResizeMode.Stretch)
        for column, width in enumerate((150, 120, 160, 90, 110, 105, 105, 95, 95, 90)):
            self.table.setColumnWidth(column, width)
        layout.addWidget(self.table, 1)

        inspect_group = QGroupBox("缓冲区检查")
        inspect_row = QHBoxLayout(inspect_group)
        normal_button = QPushButton("进入 World Normal")
        normal_button.clicked.connect(self.enter_world_normal)
        inspect_row.addWidget(normal_button)
        depth_button = QPushButton("进入自定义 Scene Depth（M_PostDepth 后期预览）")
        depth_button.clicked.connect(self.enter_scene_depth)
        inspect_row.addWidget(depth_button)
        restore_debug_button = QPushButton("恢复后期预览、玻璃母材质与 Lit")
        restore_debug_button.clicked.connect(self.restore_debug)
        inspect_row.addWidget(restore_debug_button)
        restore_original_button = QPushButton("撤销最近一次正式处理")
        restore_original_button.clicked.connect(self.restore_original)
        inspect_row.addWidget(restore_original_button)
        layout.addWidget(inspect_group)

        bottom = QHBoxLayout()
        report_button = QPushButton("导出 JSON/CSV 报告")
        report_button.clicked.connect(self.export_report)
        bottom.addWidget(report_button)
        bottom.addStretch(1)
        layout.addLayout(bottom)

        self.log = QTextEdit()
        self.log.setReadOnly(True)
        self.log.setMaximumHeight(125)
        layout.addWidget(self.log)

        self.tabs.addTab(material_page, "材质与缓冲检查")
        self.tabs.addTab(self._build_uv_page(), "一键盒体 UV")
        self.tabs.addTab(self._build_mesh_quality_page(), "模型质量检查（测试中）")
        root_layout.addWidget(self.tabs)

    def _build_uv_page(self):
        page = QWidget()
        layout = QVBoxLayout(page)

        intro = QLabel(
            "点击扫描时直接读取 World Outliner 当前选择及其子级，不需要先锁定 Root。"
            "按两条最长世界边的面积自动区分大型/小型，"
            "并直接在原 Static Mesh 的指定 UV 通道执行 Box Projection。墙体、地面等名称会优先归为大型。"
        )
        intro.setWordWrap(True)
        layout.addWidget(intro)

        settings = QGroupBox("盒体投影规则（单位：厘米）")
        settings_layout = QHBoxLayout(settings)
        settings_layout.addWidget(QLabel("大型尺寸 X/Y/Z"))
        self.uv_large_size = QDoubleSpinBox()
        self.uv_large_size.setRange(1.0, 100000.0)
        self.uv_large_size.setDecimals(1)
        self.uv_large_size.setValue(200.0)
        self.uv_large_size.setSuffix(" cm")
        self.uv_large_size.valueChanged.connect(self._save_state)
        settings_layout.addWidget(self.uv_large_size)

        settings_layout.addWidget(QLabel("小型尺寸 X/Y/Z"))
        self.uv_small_size = QDoubleSpinBox()
        self.uv_small_size.setRange(1.0, 100000.0)
        self.uv_small_size.setDecimals(1)
        self.uv_small_size.setValue(50.0)
        self.uv_small_size.setSuffix(" cm")
        self.uv_small_size.valueChanged.connect(self._save_state)
        settings_layout.addWidget(self.uv_small_size)

        settings_layout.addWidget(QLabel("大型面积阈值"))
        self.uv_area_threshold = QDoubleSpinBox()
        self.uv_area_threshold.setRange(0.0, 1000000.0)
        self.uv_area_threshold.setDecimals(2)
        self.uv_area_threshold.setValue(4.0)
        self.uv_area_threshold.setSuffix(" m²")
        self.uv_area_threshold.valueChanged.connect(self._save_state)
        settings_layout.addWidget(self.uv_area_threshold)

        settings_layout.addWidget(QLabel("目标 UV 通道"))
        self.uv_channel = QComboBox()
        for index in range(8):
            self.uv_channel.addItem("UV{}".format(index), index)
        self.uv_channel.currentIndexChanged.connect(self._save_state)
        settings_layout.addWidget(self.uv_channel)
        settings_layout.addStretch(1)
        layout.addWidget(settings)

        safety = QLabel(
            "直接覆盖模式：不生成网格副本、不占用双份空间，也不提供自动撤销。"
            "处理前请确认分类和 UV 通道；结果有问题时请重新导入或再次调整。"
        )
        safety.setWordWrap(True)
        safety.setStyleSheet("color: #ffbf69;")
        layout.addWidget(safety)

        action_row = QHBoxLayout()
        uv_scan_button = QPushButton("1. 扫描并自动分类")
        uv_scan_button.clicked.connect(self.scan_uv)
        action_row.addWidget(uv_scan_button)
        self.uv_apply_button = QPushButton("2. 直接覆盖原网格并一键投影 UV")
        self.uv_apply_button.clicked.connect(self.apply_uv)
        action_row.addWidget(self.uv_apply_button)
        action_row.addStretch(1)
        layout.addLayout(action_row)

        self.uv_summary = QLabel("尚未扫描 UV")
        self.uv_summary.setStyleSheet("font-weight: bold; color: #d0d0d0;")
        layout.addWidget(self.uv_summary)

        filter_row = QHBoxLayout()
        filter_row.addWidget(QLabel("显示筛选"))
        self.uv_filter_combo = QComboBox()
        self.uv_filter_combo.addItem("全部网格", "all")
        self.uv_filter_combo.addItem("大型表面", "large")
        self.uv_filter_combo.addItem("小型表面", "small")
        self.uv_filter_combo.addItem("已跳过", "skipped")
        self.uv_filter_combo.currentIndexChanged.connect(self._apply_uv_filter)
        filter_row.addWidget(self.uv_filter_combo)
        uv_select_filtered = QPushButton("在场景中选中筛选结果")
        uv_select_filtered.clicked.connect(self.select_filtered_uv_actors)
        filter_row.addWidget(uv_select_filtered)
        filter_row.addSpacing(20)
        filter_row.addWidget(QLabel("将表格选中行标记为"))
        mark_large = QPushButton("大型")
        mark_large.clicked.connect(lambda: self._mark_selected_uv("large"))
        filter_row.addWidget(mark_large)
        mark_small = QPushButton("小型")
        mark_small.clicked.connect(lambda: self._mark_selected_uv("small"))
        filter_row.addWidget(mark_small)
        mark_skip = QPushButton("跳过")
        mark_skip.clicked.connect(lambda: self._mark_selected_uv("skipped"))
        filter_row.addWidget(mark_skip)
        filter_row.addStretch(1)
        layout.addLayout(filter_row)

        self.uv_table = QTableWidget(0, 10)
        self.uv_table.setHorizontalHeaderLabels(
            [
                "Static Mesh",
                "Actor",
                "实例数",
                "本地尺寸(cm)",
                "世界尺寸(cm)",
                "世界缩放",
                "两大边面积(m²)",
                "分类",
                "投影尺寸",
                "依据/状态",
            ]
        )
        self.uv_table.setSelectionBehavior(
            QAbstractItemView.SelectionBehavior.SelectRows
        )
        self.uv_table.setSelectionMode(
            QAbstractItemView.SelectionMode.ExtendedSelection
        )
        self.uv_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        uv_header = self.uv_table.horizontalHeader()
        uv_header.setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        uv_header.setSectionResizeMode(9, QHeaderView.ResizeMode.Stretch)
        for column, width in enumerate((170, 180, 65, 145, 145, 120, 115, 70, 145)):
            self.uv_table.setColumnWidth(column, width)
        layout.addWidget(self.uv_table, 1)

        bottom = QHBoxLayout()
        uv_report = QPushButton("导出 UV JSON/CSV 报告")
        uv_report.clicked.connect(self.export_uv_report)
        bottom.addWidget(uv_report)
        bottom.addStretch(1)
        layout.addLayout(bottom)
        return page

    def _build_mesh_quality_page(self):
        page = QWidget()
        layout = QVBoxLayout(page)

        intro = QLabel(
            "只读检查 World Outliner 当前选择及其子级的 Static Mesh。"
            "法线与拓扑检查读取源 Static Mesh 的 MeshDescription LOD0。"
            "推荐预设会检查无效法线、连续反向区域、闭合壳体方向、"
            "退化面、局部绕序冲突、非流形拓扑以及 UV 缺失/退化和异常单向拉伸；"
            "扫描不会修改、重建或保存网格。"
            "结果先按 Actor 名称排序并折叠分组，展开 Actor 后可查看它的全部问题。"
        )
        intro.setWordWrap(True)
        layout.addWidget(intro)

        note = QLabel(
            "重要：CPU/拓扑检查不能完全等同于最终 World Normal。双面材质、Nanite、"
            "法线贴图和 WPO 仍可能改变视口结果，因此方向结果必须结合红色线框高亮复查。"
            "开放网格没有可证明的整体外侧，会作为提示输出并在默认筛选中隐藏；"
            "组件完整变换矩阵行列式小于 0 时，Actor 镜像负缩放会单独归入实例变换，"
            "不会合并成源资产法线错误；不再用 XYZ 缩放乘积代替矩阵判断。"
            "同一网格按实际世界缩放去重分析；"
            "开放墙片、玻璃、树叶等无法仅凭几何可靠判断‘整体应朝哪边’，仍需人工复查。"
            "‘组件实例数’是共享该网格与缩放组合的场景组件数量；‘命中元素数’按问题显示"
            "角点、三角面或边，不会再乘以组件实例数。扫描按网格分帧，但单个超高面网格"
            "仍需本次检查结束后才能响应取消。"
        )
        note.setWordWrap(True)
        note.setStyleSheet("color: #ffbf69;")
        layout.addWidget(note)

        settings = QGroupBox("检查设置")
        settings_row = QHBoxLayout(settings)
        settings_row.addWidget(QLabel("预设"))
        self.mesh_quality_preset = QComboBox()
        self.mesh_quality_preset.addItem("视觉宽松", "lenient")
        self.mesh_quality_preset.addItem("连续法线/拓扑检查（推荐）", "standard")
        self.mesh_quality_preset.addItem("严格数据检查（含非单位法线）", "strict")
        settings_row.addWidget(self.mesh_quality_preset)

        settings_row.addWidget(QLabel("检查 UV 通道"))
        self.mesh_quality_uv_channel = QComboBox()
        for index in range(8):
            self.mesh_quality_uv_channel.addItem("UV{}".format(index), index)
        settings_row.addWidget(self.mesh_quality_uv_channel)

        settings_row.addWidget(QLabel("拉伸警告 ≥"))
        self.mesh_quality_stretch_warning = QDoubleSpinBox()
        self.mesh_quality_stretch_warning.setRange(1.0, 1000.0)
        self.mesh_quality_stretch_warning.setDecimals(1)
        self.mesh_quality_stretch_warning.setValue(3.0)
        self.mesh_quality_stretch_warning.setSuffix(" 倍")
        settings_row.addWidget(self.mesh_quality_stretch_warning)

        settings_row.addWidget(QLabel("严重错误 ≥"))
        self.mesh_quality_stretch_error = QDoubleSpinBox()
        self.mesh_quality_stretch_error.setRange(1.0, 1000.0)
        self.mesh_quality_stretch_error.setDecimals(1)
        self.mesh_quality_stretch_error.setValue(6.0)
        self.mesh_quality_stretch_error.setSuffix(" 倍")
        settings_row.addWidget(self.mesh_quality_stretch_error)

        self.mesh_quality_include_scale = QCheckBox("检查完整矩阵负行列式")
        self.mesh_quality_include_scale.setToolTip(
            "由原生接口读取每个 Static Mesh Component 的完整 ComponentToWorld 矩阵；"
            "仅在 determinant < 0 时报告 Actor 镜像负缩放。"
        )
        self.mesh_quality_include_scale.setChecked(True)
        settings_row.addWidget(self.mesh_quality_include_scale)
        settings_row.addStretch(1)
        layout.addWidget(settings)

        self.mesh_quality_preset.currentIndexChanged.connect(
            self._mesh_quality_preset_changed
        )
        self.mesh_quality_uv_channel.currentIndexChanged.connect(self._save_state)
        self.mesh_quality_stretch_warning.valueChanged.connect(self._save_state)
        self.mesh_quality_stretch_error.valueChanged.connect(self._save_state)
        self.mesh_quality_include_scale.stateChanged.connect(self._save_state)

        action_row = QHBoxLayout()
        self.mesh_quality_scan_button = QPushButton(
            "1. 按网格分帧扫描当前选择及子级（只读）"
        )
        self.mesh_quality_scan_button.setToolTip(
            "每帧检查一个网格；单个超高面网格仍需完整检查结束后才能响应取消。"
        )
        self.mesh_quality_scan_button.clicked.connect(self.scan_mesh_quality)
        action_row.addWidget(self.mesh_quality_scan_button)
        locate_actor = QPushButton("在场景中定位选中结果")
        locate_actor.clicked.connect(self.locate_mesh_quality_actors)
        action_row.addWidget(locate_actor)
        self.mesh_quality_locate_asset_button = QPushButton(
            "在 Content Browser 定位网格"
        )
        self.mesh_quality_locate_asset_button.clicked.connect(
            self.locate_mesh_quality_assets
        )
        action_row.addWidget(self.mesh_quality_locate_asset_button)
        self.mesh_quality_preview_button = QPushButton(
            "红色线框高亮错误区域"
        )
        self.mesh_quality_preview_button.setToolTip(
            "优先按完整 Source LOD0 Error Triangle IDs 高亮所选错误区域；"
            "选择 Actor 行可高亮其下全部可定位区域。安全上限为 100000 个组件实例面，"
            "超过时会明确提示并截断。"
        )
        self.mesh_quality_preview_button.clicked.connect(
            self.preview_next_mesh_quality_sample
        )
        action_row.addWidget(self.mesh_quality_preview_button)
        clear_preview = QPushButton("清除视口标记")
        clear_preview.clicked.connect(self.clear_mesh_quality_preview)
        action_row.addWidget(clear_preview)
        action_row.addStretch(1)
        layout.addLayout(action_row)

        self.mesh_quality_summary = QLabel("尚未扫描模型质量")
        self.mesh_quality_summary.setToolTip(
            "错误、警告、法线和 UV 的数字都表示聚合后的结果组数量，"
            "不是组件数或命中元素数；同一聚合结果被多个 Actor 使用时，会分别显示在对应 Actor 下。"
        )
        self.mesh_quality_summary.setStyleSheet(
            "font-weight: bold; color: #d0d0d0;"
        )
        layout.addWidget(self.mesh_quality_summary)

        filter_row = QHBoxLayout()
        filter_row.addWidget(QLabel("严重程度"))
        self.mesh_quality_severity_filter = QComboBox()
        self.mesh_quality_severity_filter.addItem("错误 + 警告（默认）", "actionable")
        self.mesh_quality_severity_filter.addItem("全部（含提示）", "all")
        self.mesh_quality_severity_filter.addItem("仅错误", "error")
        self.mesh_quality_severity_filter.addItem("仅警告", "warning")
        self.mesh_quality_severity_filter.addItem("仅提示", "info")
        self.mesh_quality_severity_filter.currentIndexChanged.connect(
            self._apply_mesh_quality_filter
        )
        filter_row.addWidget(self.mesh_quality_severity_filter)
        filter_row.addWidget(QLabel("问题类别"))
        self.mesh_quality_category_filter = QComboBox()
        self.mesh_quality_category_filter.addItem("全部", "all")
        self.mesh_quality_category_filter.addItem("法线", "normal")
        self.mesh_quality_category_filter.addItem("UV", "uv")
        self.mesh_quality_category_filter.addItem("几何/拓扑", "geometry")
        self.mesh_quality_category_filter.addItem("实例变换", "transform")
        self.mesh_quality_category_filter.addItem("扫描失败", "system")
        self.mesh_quality_category_filter.currentIndexChanged.connect(
            self._apply_mesh_quality_filter
        )
        filter_row.addWidget(self.mesh_quality_category_filter)
        expand_actors = QPushButton("展开全部 Actor")
        expand_actors.clicked.connect(self._expand_all_mesh_quality_actors)
        filter_row.addWidget(expand_actors)
        collapse_actors = QPushButton("折叠全部 Actor")
        collapse_actors.clicked.connect(self._collapse_all_mesh_quality_actors)
        filter_row.addWidget(collapse_actors)
        filter_row.addStretch(1)
        export = QPushButton("导出模型质量 JSON/CSV 报告")
        export.clicked.connect(self.export_mesh_quality_report)
        filter_row.addWidget(export)
        layout.addLayout(filter_row)

        self.mesh_quality_table = _MeshQualityTreeWidget()
        self.mesh_quality_table.setColumnCount(10)
        self.mesh_quality_table.setHorizontalHeaderLabels(
            [
                "Actor / 级别",
                "问题类型",
                "Static Mesh",
                "Actor 路径",
                "组件实例数",
                "世界缩放",
                "材质槽",
                "命中元素数",
                "最大指标",
                "说明",
            ]
        )
        self.mesh_quality_table.horizontalHeaderItem(4).setToolTip(
            "当前结果组引用的 Static Mesh Component 数量；不是错误数量。"
        )
        self.mesh_quality_table.horizontalHeaderItem(7).setToolTip(
            "当前结果组命中的网格元素数。无效法线按角点，顶点法线反向、"
            "面/UV 问题及局部绕序冲突按三角面，非流形拓扑按边；"
            "Actor 负缩放与扫描失败显示‘—’。不会乘以组件实例数。"
        )
        self.mesh_quality_table.setSelectionBehavior(
            QAbstractItemView.SelectionBehavior.SelectRows
        )
        self.mesh_quality_table.setSelectionMode(
            QAbstractItemView.SelectionMode.ExtendedSelection
        )
        self.mesh_quality_table.setEditTriggers(
            QAbstractItemView.EditTrigger.NoEditTriggers
        )
        self.mesh_quality_table.setRootIsDecorated(True)
        self.mesh_quality_table.setItemsExpandable(True)
        self.mesh_quality_table.setExpandsOnDoubleClick(True)
        self.mesh_quality_table.setUniformRowHeights(True)
        quality_header = self.mesh_quality_table.horizontalHeader()
        quality_header.setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        quality_header.setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        quality_header.setSectionResizeMode(9, QHeaderView.ResizeMode.Stretch)
        for column, width in enumerate((190, 135, 180, 210, 95, 130, 60, 105, 90)):
            self.mesh_quality_table.setColumnWidth(column, width)
        self.mesh_quality_table.itemDoubleClicked.connect(
            self._mesh_quality_item_double_clicked
        )
        layout.addWidget(self.mesh_quality_table, 1)

        self.mesh_quality_log = QTextEdit()
        self.mesh_quality_log.setReadOnly(True)
        self.mesh_quality_log.setMaximumHeight(105)
        layout.addWidget(self.mesh_quality_log)
        return page

    def _load_state(self):
        state = core.load_ui_state()
        self.glass_path.setText(state.get("glass_material_path", ""))
        self.post_depth_path.setText(
            state.get("post_depth_material_path", core.DEFAULT_POST_DEPTH_MATERIAL)
        )
        self.normal_function_path.setText(
            state.get("normal_function_path", "")
        )
        self.auto_two_sided.setChecked(state.get("auto_two_sided", True))
        self.uv_large_size.setValue(float(state.get("uv_large_size", 200.0)))
        self.uv_small_size.setValue(float(state.get("uv_small_size", 50.0)))
        self.uv_area_threshold.setValue(
            float(state.get("uv_area_threshold_m2", 4.0))
        )
        channel = int(state.get("uv_channel", 0))
        channel_index = self.uv_channel.findData(channel)
        if channel_index >= 0:
            self.uv_channel.setCurrentIndex(channel_index)
        quality_preset = str(state.get("mesh_quality_preset", "standard"))
        quality_index = self.mesh_quality_preset.findData(quality_preset)
        if quality_index < 0:
            quality_preset = "standard"
            quality_index = self.mesh_quality_preset.findData(quality_preset)
        self.mesh_quality_preset.blockSignals(True)
        if quality_index >= 0:
            self.mesh_quality_preset.setCurrentIndex(quality_index)
        self.mesh_quality_preset.blockSignals(False)
        preset_options = mesh_quality.options_for_preset(quality_preset)
        quality_channel = int(state.get("mesh_quality_uv_channel", 0))
        quality_channel_index = self.mesh_quality_uv_channel.findData(quality_channel)
        if quality_channel_index >= 0:
            self.mesh_quality_uv_channel.setCurrentIndex(quality_channel_index)
        self.mesh_quality_stretch_warning.setValue(
            float(
                state.get(
                    "mesh_quality_stretch_warning",
                    preset_options.uv_stretch_warning_threshold,
                )
            )
        )
        self.mesh_quality_stretch_error.setValue(
            float(
                state.get(
                    "mesh_quality_stretch_error",
                    preset_options.uv_stretch_error_threshold,
                )
            )
        )
        self.mesh_quality_include_scale.setChecked(
            state.get("mesh_quality_include_scale", True)
        )

    def _save_state(self, *_args):
        try:
            core.save_ui_state(
                {
                    "glass_material_path": self.glass_path.text().strip(),
                    "post_depth_material_path": self.post_depth_path.text().strip(),
                    "normal_function_path": self.normal_function_path.text().strip(),
                    "auto_two_sided": self.auto_two_sided.isChecked(),
                    "uv_large_size": self.uv_large_size.value(),
                    "uv_small_size": self.uv_small_size.value(),
                    "uv_area_threshold_m2": self.uv_area_threshold.value(),
                    "uv_channel": int(self.uv_channel.currentData() or 0),
                    "mesh_quality_preset": str(
                        self.mesh_quality_preset.currentData() or "standard"
                    ),
                    "mesh_quality_uv_channel": int(
                        self.mesh_quality_uv_channel.currentData() or 0
                    ),
                    "mesh_quality_stretch_warning": self.mesh_quality_stretch_warning.value(),
                    "mesh_quality_stretch_error": self.mesh_quality_stretch_error.value(),
                    "mesh_quality_include_scale": self.mesh_quality_include_scale.isChecked(),
                }
            )
            return True
        except Exception as exc:
            # UI preferences are non-critical.  A storage problem must be
            # visible, but it must not make scan buttons appear unresponsive.
            self._message(
                "保存界面设置失败，当前操作仍会继续：{}".format(exc), True
            )
            return False

    def _message(self, text: str, error: bool = False):
        self.log.append(text)
        if error:
            unreal.log_error("[MaterialAuditor] {}".format(text))
        else:
            unreal.log("[MaterialAuditor] {}".format(text))

    def _mesh_quality_message(self, text: str, error: bool = False):
        self.mesh_quality_log.append(text)
        if error:
            unreal.log_error("[MeshQuality] {}".format(text))
        else:
            unreal.log("[MeshQuality] {}".format(text))

    def _mesh_quality_preset_changed(self, *_args):
        preset = str(self.mesh_quality_preset.currentData() or "standard")
        options = mesh_quality.options_for_preset(preset)
        self.mesh_quality_stretch_warning.blockSignals(True)
        self.mesh_quality_stretch_error.blockSignals(True)
        self.mesh_quality_stretch_warning.setValue(
            options.uv_stretch_warning_threshold
        )
        self.mesh_quality_stretch_error.setValue(options.uv_stretch_error_threshold)
        self.mesh_quality_stretch_warning.blockSignals(False)
        self.mesh_quality_stretch_error.blockSignals(False)
        self._save_state()

    def _mesh_quality_options(self):
        preset = str(self.mesh_quality_preset.currentData() or "standard")
        options = mesh_quality.options_for_preset(preset)
        warning = float(self.mesh_quality_stretch_warning.value())
        error = float(self.mesh_quality_stretch_error.value())
        if error <= warning:
            raise RuntimeError("UV 严重错误阈值必须大于拉伸警告阈值。")
        options.uv_channel = int(self.mesh_quality_uv_channel.currentData() or 0)
        options.uv_stretch_warning_threshold = warning
        options.uv_stretch_error_threshold = error
        options.include_scale_issues = self.mesh_quality_include_scale.isChecked()
        return options

    def _unregister_mesh_quality_tick(self):
        handle = self._mesh_quality_tick_handle
        if handle is None:
            return
        try:
            unreal.unregister_slate_post_tick_callback(handle)
            self._mesh_quality_tick_handle = None
        except Exception as exc:
            unreal.log_warning(
                "[MeshQuality] 注销分帧扫描回调失败（通常可忽略）：{}".format(exc)
            )

    def scan_mesh_quality(self):
        if self._mesh_quality_job_state is not None or self._scan_active:
            QMessageBox.information(self, "模型质量检查", "已有任务正在运行。")
            return
        try:
            options = self._mesh_quality_options()
            if not mesh_quality.native_audit_available():
                raise mesh_quality.NativeAuditUnavailableError(
                    "原生模型审计模块尚未加载。请确认 {} 插件已经完整编译并重启 UE。".format(
                        version.PLUGIN_VERSION
                    )
                )
        except Exception as exc:
            QMessageBox.critical(self, "模型质量检查", str(exc))
            self._mesh_quality_message(str(exc), True)
            return

        self._save_state()
        self.clear_mesh_quality_preview(log_message=False)
        self._begin_scan("模型质量")
        self.mesh_quality_rows = []
        self._mesh_quality_scan_summary = {}
        self.mesh_quality_table.clear()
        self.mesh_quality_summary.setText("正在读取当前选择及其子级…")
        try:
            groups = mesh_quality.collect_selected_mesh_groups(self._scan_progress)
        except mesh_quality.MeshQualityScanCancelled as exc:
            self._mesh_quality_message(str(exc))
            self._end_scan()
            return
        except Exception as exc:
            self._mesh_quality_message("收集扫描范围失败：{}".format(exc), True)
            unreal.log_error(traceback.format_exc())
            self._end_scan()
            QMessageBox.critical(self, "模型质量检查", str(exc))
            return

        if self._closing or self._scan_cancel_requested:
            self._end_scan()
            return

        self._mesh_quality_job_state = {
            "groups": groups,
            "options": options,
            "index": 0,
            "rows": [],
            "busy": False,
            "fatal_error": "",
        }
        try:
            self._mesh_quality_tick_handle = unreal.register_slate_post_tick_callback(
                self._mesh_quality_scan_tick
            )
        except Exception as exc:
            self._mesh_quality_job_state = None
            self._end_scan()
            self._mesh_quality_message("无法启动分帧扫描：{}".format(exc), True)
            QMessageBox.critical(self, "模型质量检查", str(exc))
            return
        self._mesh_quality_message(
            "已收集 {} 个唯一 Static Mesh + 世界缩放组合，开始逐网格只读检查。".format(
                len(groups)
            )
        )

    def _mesh_quality_scan_tick(self, _delta_seconds=0.0):
        state = self._mesh_quality_job_state
        if state is None or state.get("busy"):
            return
        if self._scan_cancel_requested:
            self._finish_mesh_quality_scan(cancelled=True)
            return
        index = state["index"]
        groups = state["groups"]
        total = len(groups)
        if index >= total:
            self._finish_mesh_quality_scan(cancelled=False)
            return

        group = groups[index]
        state["busy"] = True
        self.scan_progress_label.setText(
            "模型质量：{} / {}，正在检查 {}".format(
                index + 1, total, group.mesh_name
            )
        )
        try:
            state["rows"].extend(
                mesh_quality.analyze_mesh_groups([group], state["options"])
            )
            state["index"] += 1
        except Exception as exc:
            state["fatal_error"] = str(exc)
            unreal.log_error(traceback.format_exc())
        finally:
            state["busy"] = False

        if state["fatal_error"]:
            self._finish_mesh_quality_scan(cancelled=False)

    def _finish_mesh_quality_scan(self, cancelled: bool = False):
        state = self._mesh_quality_job_state
        if state is None:
            self._unregister_mesh_quality_tick()
            return
        self._unregister_mesh_quality_tick()
        self._mesh_quality_job_state = None
        self._mesh_quality_scanned_groups = int(state.get("index", 0))
        self._mesh_quality_scan_summary = mesh_quality.summarize_audit_groups(
            list(state.get("groups", []))[: self._mesh_quality_scanned_groups]
        )
        priority = {"error": 0, "warning": 1, "info": 2}
        self.mesh_quality_rows = sorted(
            state.get("rows", []),
            key=lambda row: (
                priority.get(row.severity, 9),
                row.mesh_name.lower(),
                row.issue_code,
                row.lod_index,
                row.region_index,
                row.region_id,
                row.world_scale,
            ),
        )
        self._populate_mesh_quality_table()
        self._end_scan()
        fatal_error = str(state.get("fatal_error") or "")
        if fatal_error:
            self._mesh_quality_message("扫描中止：{}".format(fatal_error), True)
            QMessageBox.critical(self, "模型质量检查", fatal_error)
            return
        suffix = "，未开始的网格未检查" if cancelled else ""
        abstained = int(
            self._mesh_quality_scan_summary.get(
                "closed_shell_direction_abstained_count", 0
            )
            or 0
        )
        direction_suffix = (
            "；方向检查未覆盖 {} 个封闭壳体，请查看报告中的弃权原因".format(
                abstained
            )
            if abstained
            else ""
        )
        self._mesh_quality_message(
            "扫描完成：已检查 {} / {} 个组合，发现 {} 组问题{}{}。".format(
                self._mesh_quality_scanned_groups,
                len(state.get("groups", [])),
                len(self.mesh_quality_rows),
                suffix,
                direction_suffix,
            )
        )

    @staticmethod
    def _mesh_quality_fallback_actor_label(actor_path):
        token = str(actor_path or "").rstrip("./")
        return token.rsplit(".", 1)[-1] if token else "未知 Actor"

    def _mesh_quality_actor_label_map(self):
        """Resolve labels by actor path without relying on two independently sorted lists."""

        wanted_paths = {
            path
            for record in self.mesh_quality_rows
            for path in record.actor_paths
            if str(path or "").strip()
        }
        labels = {}
        for record in self.mesh_quality_rows:
            if record.actor_path and record.actor_label:
                labels.setdefault(record.actor_path, record.actor_label)
        if wanted_paths:
            try:
                subsystem = unreal.get_editor_subsystem(unreal.EditorActorSubsystem)
                for actor in subsystem.get_all_level_actors():
                    try:
                        actor_path = str(actor.get_path_name())
                    except Exception:
                        continue
                    if actor_path not in wanted_paths:
                        continue
                    try:
                        actor_label = str(actor.get_actor_label())
                    except Exception:
                        try:
                            actor_label = str(actor.get_name())
                        except Exception:
                            actor_label = ""
                    if actor_label:
                        labels[actor_path] = actor_label
            except Exception as exc:
                unreal.log_warning(
                    "[MeshQualityUI] 读取 Actor 标签失败，改用对象路径：{}".format(exc)
                )
        return {
            path: labels.get(path) or self._mesh_quality_fallback_actor_label(path)
            for path in wanted_paths
        }

    @staticmethod
    def _mesh_quality_record_for_actor(record, actor_path, actor_label):
        """Create an actor-scoped UI view of an aggregated mesh result row."""

        component_paths = [
            path
            for path in record.component_paths
            if str(path).startswith(str(actor_path) + ".")
        ]
        if not component_paths and record.actor_path == actor_path:
            component_paths = [record.component_path] if record.component_path else []
        component_names = [
            str(path).rsplit(".", 1)[-1] for path in component_paths if path
        ]
        component_path = component_paths[0] if component_paths else ""
        component_name = component_names[0] if component_names else ""
        return replace(
            record,
            actor_path=actor_path,
            actor_label=actor_label,
            component_path=component_path,
            component_name=component_name,
            actor_paths=[actor_path],
            actor_labels=[actor_label],
            component_paths=component_paths,
            component_names=component_names,
            component_count=max(1, len(component_paths)),
        )

    @staticmethod
    def _mesh_quality_category_count_text(records):
        counts = {key: 0 for key, _label in _MESH_QUALITY_CATEGORY_SUMMARY}
        for record in records:
            category = str(getattr(record, "category", "") or "")
            if category in counts:
                counts[category] += 1
        return "｜".join(
            "{} {}".format(label, counts[key])
            for key, label in _MESH_QUALITY_CATEGORY_SUMMARY
        )

    @staticmethod
    def _mesh_quality_complete_triangle_ids(record):
        complete = list(getattr(record, "error_triangle_ids", None) or [])
        if complete:
            return complete
        return list(getattr(record, "triangle_ids", None) or [])

    @classmethod
    def _mesh_quality_triangle_ids_text(cls, record, limit=24):
        triangle_ids = cls._mesh_quality_complete_triangle_ids(record)
        if not triangle_ids:
            return "未提供"
        shown = triangle_ids[: max(1, int(limit))]
        text = ", ".join(str(value) for value in shown)
        if len(triangle_ids) > len(shown):
            text += " …（显示前 {} / 共 {}）".format(
                len(shown), len(triangle_ids)
            )
        return text

    @classmethod
    def _mesh_quality_region_text(cls, record):
        category = str(getattr(record, "category", "") or "").lower()
        if category == "transform":
            return "实例变换"
        if category == "system":
            return "扫描状态"
        try:
            region_index = int(getattr(record, "region_index", -1))
        except Exception:
            region_index = -1
        region_id = str(getattr(record, "region_id", "") or "")
        if region_index >= 0:
            return "区域 {}".format(region_index + 1)
        if region_id:
            return "区域 {}".format(region_id)
        return "兼容结果组"

    @classmethod
    def _mesh_quality_region_stats(cls, record):
        complete_ids = cls._mesh_quality_complete_triangle_ids(record)
        complete_count = len(set(complete_ids))
        sample_count = len(getattr(record, "sample_vertices", None) or []) // 3
        truncated = bool(getattr(record, "samples_truncated", False)) or (
            complete_count > sample_count
        )
        return complete_count, sample_count, truncated

    def _populate_mesh_quality_table(self):
        self.mesh_quality_table.clear()
        colors = {
            "error": QColor(255, 110, 110),
            "warning": QColor(255, 191, 105),
            "info": QColor(160, 190, 225),
        }
        priority = {"error": 0, "warning": 1, "info": 2}
        actor_labels = self._mesh_quality_actor_label_map()
        actor_groups = {}
        for record in self.mesh_quality_rows:
            actor_paths = list(record.actor_paths)
            if not actor_paths and record.actor_path:
                actor_paths = [record.actor_path]
            if not actor_paths:
                actor_paths = ["<无可用 Actor 路径>"]
            for actor_path in actor_paths:
                actor_label = actor_labels.get(actor_path) or (
                    record.actor_label
                    if record.actor_path == actor_path and record.actor_label
                    else self._mesh_quality_fallback_actor_label(actor_path)
                )
                actor_groups.setdefault(actor_path, {"label": actor_label, "rows": []})[
                    "rows"
                ].append(
                    self._mesh_quality_record_for_actor(
                        record, actor_path, actor_label
                    )
                )

        for actor_path, group in sorted(
            actor_groups.items(),
            key=lambda item: (item[1]["label"].casefold(), item[0].casefold()),
        ):
            group_rows = sorted(
                group["rows"],
                key=lambda record: (
                    priority.get(record.severity, 9),
                    record.mesh_name.casefold(),
                    record.issue_code,
                    record.lod_index,
                    record.region_index,
                    record.region_id,
                    record.world_scale,
                ),
            )
            error_count = sum(row.severity == "error" for row in group_rows)
            warning_count = sum(row.severity == "warning" for row in group_rows)
            mesh_count = len({row.mesh_path for row in group_rows if row.mesh_path})
            component_count = len(
                {
                    path
                    for row in group_rows
                    for path in row.component_paths
                    if path
                }
            )
            actor_values = [
                group["label"],
                "{} 个问题".format(len(group_rows)),
                "{} 个网格".format(mesh_count),
                actor_path,
                str(component_count),
                "",
                "",
                "",
                "",
                "错误 {}｜警告 {}｜{}｜双击箭头展开或折叠".format(
                    error_count,
                    warning_count,
                    self._mesh_quality_category_count_text(group_rows),
                ),
            ]
            actor_item = QTreeWidgetItem([str(value) for value in actor_values])
            actor_item.setData(0, _MESH_QUALITY_ITEM_KIND_ROLE, "actor")
            actor_item.setToolTip(0, actor_path)
            actor_item.setToolTip(3, actor_path)
            actor_font = actor_item.font(0)
            actor_font.setBold(True)
            actor_item.setFont(0, actor_font)
            actor_severity = "error" if error_count else (
                "warning" if warning_count else "info"
            )
            actor_item.setForeground(
                0, colors.get(actor_severity, QColor(210, 210, 210))
            )
            self.mesh_quality_table.addTopLevelItem(actor_item)

            for record in group_rows:
                self._add_mesh_quality_issue_item(actor_item, record, colors)
            actor_item.setExpanded(False)

        self._apply_mesh_quality_filter()
        self._update_mesh_quality_summary()

    def _add_mesh_quality_issue_item(self, actor_item, record, colors):
        scale_text = "{:.4g} × {:.4g} × {:.4g}".format(*record.world_scale)
        metric_text = "{:.4g}".format(record.max_metric)
        if record.issue_code == "UV_STRETCH":
            metric_text += " 倍"
        affected_text = mesh_quality.format_affected_elements(
            record.issue_code,
            record.affected_count,
            record.category,
        )
        affected_unit = mesh_quality.affected_element_unit(
            record.issue_code, record.category
        )
        material_slot_text = (
            "全部" if record.material_slot < 0 else str(record.material_slot)
        )
        triangle_ids_text = self._mesh_quality_triangle_ids_text(record)
        region_text = self._mesh_quality_region_text(record)
        complete_face_count, sample_face_count, sample_truncated = (
            self._mesh_quality_region_stats(record)
        )
        lod_text = "LOD{}".format(max(0, int(getattr(record, "lod_index", 0))))
        triangle_id_space = str(
            getattr(record, "triangle_id_space", "") or "未标注 ID 空间"
        )
        has_native_region_fields = bool(
            record.details.get("native_region_fields", False)
        )
        if has_native_region_fields:
            complete_faces_text = "{} 个完整区域面".format(complete_face_count)
        else:
            complete_faces_text = "兼容预览 {} 面（旧 DLL 未提供完整区域）".format(
                complete_face_count
            )
        sample_status_text = "原生预览数据 {} 面{}".format(
            sample_face_count,
            "（已截断）" if sample_truncated else "",
        )
        shown_actor_paths = record.actor_paths[:20]
        shown_component_paths = record.component_paths[:20]
        omitted_actors = len(record.actor_paths) - len(shown_actor_paths)
        omitted_components = len(record.component_paths) - len(
            shown_component_paths
        )
        normal_source = str(record.details.get("normal_data_source") or "")
        normal_source_label = {
            "source_mesh_description_lod0": "源 MeshDescription LOD0",
            "render_lod0": "最终渲染 LOD0",
            "built_clone_fallback": "UE 构建流程内存副本（安全回退）",
        }.get(normal_source, normal_source or "未知")
        detail_tooltip = (
            "网格：{}\n{}｜{}｜{}｜{}\nDetail Code：{}\nActor：{}{}\n组件：{}{}\n材质槽：{}\n"
            "Triangle ID 空间：{}\nError Triangle IDs / Triangle IDs：{}\n法线来源：{}\n{}"
        ).format(
            record.mesh_path,
            region_text,
            lod_text,
            complete_faces_text,
            sample_status_text,
            record.detail_code or "未提供",
            "\n".join(shown_actor_paths),
            "\n……另有 {} 个".format(omitted_actors) if omitted_actors else "",
            "\n".join(shown_component_paths),
            "\n……另有 {} 个".format(omitted_components)
            if omitted_components
            else "",
            material_slot_text,
            triangle_id_space,
            triangle_ids_text,
            normal_source_label,
            record.message,
        )
        component_tooltip = (
            "组件实例数：{}\n表示此结果组覆盖的场景 Static Mesh Component 数量；"
            "共享网格不会被重复分析。\n{}｜{}\n材质槽：{}\nTriangle IDs：{}"
        ).format(
            record.component_count,
            complete_faces_text,
            sample_status_text,
            material_slot_text,
            triangle_ids_text,
        )
        if affected_unit:
            affected_tooltip = (
                "命中元素数：{}\n本问题按‘{}’计数；这是源网格命中的元素数，"
                "不会乘以 {} 个组件实例。\n{}｜{}\n材质槽：{}\nTriangle IDs：{}"
            ).format(
                affected_text,
                affected_unit,
                record.component_count,
                complete_faces_text,
                sample_status_text,
                material_slot_text,
                triangle_ids_text,
            )
        else:
            affected_tooltip = (
                "此结果描述整个实例变换或扫描状态，没有对应的角点、三角面或边，"
                "因此显示‘—’。\n{}｜{}\n材质槽：{}\nTriangle IDs：{}"
            ).format(
                complete_faces_text,
                sample_status_text,
                material_slot_text,
                triangle_ids_text,
            )
        description_text = "{}｜{}｜{}｜{}｜材质槽 {}｜Triangle IDs {}｜{}".format(
            region_text,
            lod_text,
            complete_faces_text,
            sample_status_text,
            material_slot_text,
            triangle_ids_text,
            record.message,
        )
        issue_label = _MESH_QUALITY_ISSUE_LABELS.get(
            record.issue_code, record.issue_code
        )
        values = [
            _MESH_QUALITY_SEVERITY_LABELS.get(record.severity, record.severity),
            "{}｜{}".format(region_text, issue_label),
            "{} ({})".format(record.mesh_name, lod_text),
            record.actor_path,
            str(record.component_count),
            scale_text,
            material_slot_text,
            affected_text,
            metric_text,
            description_text,
        ]
        issue_item = QTreeWidgetItem([str(value) for value in values])
        issue_item.setData(0, _MESH_QUALITY_ITEM_KIND_ROLE, "issue")
        issue_item.setData(0, _MESH_QUALITY_RECORD_ROLE, record)
        for column, _value in enumerate(values):
            if column in (0, 1, 2, 3, 6, 8, 9):
                issue_item.setToolTip(column, detail_tooltip)
            elif column == 4:
                issue_item.setToolTip(column, component_tooltip)
            elif column == 7:
                issue_item.setToolTip(column, affected_tooltip)
            if column in (0, 1):
                issue_item.setForeground(
                    column,
                    colors.get(record.severity, QColor(210, 210, 210)),
                )
        actor_item.addChild(issue_item)

    def _update_mesh_quality_summary(self):
        errors = sum(row.severity == "error" for row in self.mesh_quality_rows)
        warnings = sum(row.severity == "warning" for row in self.mesh_quality_rows)
        normals = sum(row.category == "normal" for row in self.mesh_quality_rows)
        uvs = sum(row.category == "uv" for row in self.mesh_quality_rows)
        direction_candidates = int(
            self._mesh_quality_scan_summary.get(
                "closed_shell_direction_candidate_count", 0
            )
            or 0
        )
        direction_evaluated = int(
            self._mesh_quality_scan_summary.get(
                "closed_shell_direction_evaluated_count", 0
            )
            or 0
        )
        direction_abstained = int(
            self._mesh_quality_scan_summary.get(
                "closed_shell_direction_abstained_count", 0
            )
            or 0
        )
        direction_text = (
            "｜封闭壳体方向 候选 {} / 已判断 {} / 未覆盖 {}".format(
                direction_candidates,
                direction_evaluated,
                direction_abstained,
            )
        )
        if direction_abstained:
            direction_text += "（不能视为完全通过）"
        self.mesh_quality_summary.setText(
            "已检查 {} 个网格/缩放组合｜涉及 Actor {}｜错误结果组 {}｜警告结果组 {}｜"
            "法线结果组 {}｜UV 结果组 {}｜总结果组 {}{}".format(
                self._mesh_quality_scanned_groups,
                self.mesh_quality_table.topLevelItemCount(),
                errors,
                warnings,
                normals,
                uvs,
                len(self.mesh_quality_rows),
                direction_text,
            )
        )

    def _apply_mesh_quality_filter(self, *_args):
        self.mesh_quality_table.clearSelection()
        severity = str(self.mesh_quality_severity_filter.currentData() or "all")
        category = str(self.mesh_quality_category_filter.currentData() or "all")
        for actor_index in range(self.mesh_quality_table.topLevelItemCount()):
            actor_item = self.mesh_quality_table.topLevelItem(actor_index)
            visible_count = 0
            visible_records = []
            for child_index in range(actor_item.childCount()):
                issue_item = actor_item.child(child_index)
                record = issue_item.data(0, _MESH_QUALITY_RECORD_ROLE)
                if record:
                    severity_matches = (
                        severity == "all"
                        or (
                            severity == "actionable"
                            and record.severity in {"error", "warning"}
                        )
                        or record.severity == severity
                    )
                    visible = severity_matches and (
                        category == "all" or record.category == category
                    )
                else:
                    visible = False
                issue_item.setHidden(not visible)
                visible_count += int(visible)
                if visible:
                    visible_records.append(record)
            actor_item.setHidden(visible_count == 0)
            total_count = actor_item.childCount()
            actor_item.setText(
                1,
                "{} 个问题".format(total_count)
                if visible_count == total_count
                else "显示 {} / {} 个问题".format(visible_count, total_count),
            )
            visible_errors = sum(
                record.severity == "error" for record in visible_records
            )
            visible_warnings = sum(
                record.severity == "warning" for record in visible_records
            )
            category_text = self._mesh_quality_category_count_text(visible_records)
            actor_summary = "当前匹配：错误 {}｜警告 {}｜{}".format(
                visible_errors, visible_warnings, category_text
            )
            actor_item.setText(9, actor_summary)
            actor_item.setToolTip(
                9, actor_summary + "\n点击左侧箭头或双击 Actor 可展开/折叠问题。"
            )

    def _expand_all_mesh_quality_actors(self):
        for index in range(self.mesh_quality_table.topLevelItemCount()):
            item = self.mesh_quality_table.topLevelItem(index)
            if not item.isHidden():
                item.setExpanded(True)

    def _collapse_all_mesh_quality_actors(self):
        for index in range(self.mesh_quality_table.topLevelItemCount()):
            self.mesh_quality_table.topLevelItem(index).setExpanded(False)

    @staticmethod
    def _mesh_quality_row_identity(row):
        return (
            row.actor_path,
            row.mesh_path,
            row.issue_code,
            row.detail_code,
            row.material_slot,
            row.lod_index,
            row.region_id,
            row.region_index,
            row.triangle_id_space,
            tuple(row.world_scale),
            row.message,
        )

    def _visible_mesh_quality_rows(self):
        rows = []
        for actor_index in range(self.mesh_quality_table.topLevelItemCount()):
            actor_item = self.mesh_quality_table.topLevelItem(actor_index)
            if actor_item.isHidden():
                continue
            for child_index in range(actor_item.childCount()):
                issue_item = actor_item.child(child_index)
                if issue_item.isHidden():
                    continue
                record = issue_item.data(0, _MESH_QUALITY_RECORD_ROLE)
                if record:
                    rows.append(record)
        return rows

    def _selected_mesh_quality_rows(self):
        rows = []
        seen = set()
        for item in self.mesh_quality_table.selectedItems():
            if item.isHidden():
                continue
            kind = item.data(0, _MESH_QUALITY_ITEM_KIND_ROLE)
            if kind == "actor":
                candidates = [
                    item.child(index).data(0, _MESH_QUALITY_RECORD_ROLE)
                    for index in range(item.childCount())
                    if not item.child(index).isHidden()
                ]
            elif kind == "issue":
                candidates = [item.data(0, _MESH_QUALITY_RECORD_ROLE)]
            else:
                candidates = []
            for record in candidates:
                if not record:
                    continue
                identity = self._mesh_quality_row_identity(record)
                if identity in seen:
                    continue
                seen.add(identity)
                rows.append(record)
        return rows

    def _mesh_quality_action_rows(self):
        return self._selected_mesh_quality_rows()

    def locate_mesh_quality_actors(self):
        rows = self._mesh_quality_action_rows()
        if not rows:
            QMessageBox.information(
                self, "定位模型质量结果", "请先在结果树中选择 Actor 或一条/多条问题。"
            )
            return
        try:
            count = mesh_quality.select_mesh_quality_row_actors(
                rows, focus_viewport=True
            )
            self._mesh_quality_message("已在场景中定位 {} 个 Actor。".format(count))
        except Exception as exc:
            self._mesh_quality_message("定位 Actor 失败：{}".format(exc), True)
            QMessageBox.warning(self, "定位模型质量结果", str(exc))

    def locate_mesh_quality_assets(self):
        rows = self._mesh_quality_action_rows()
        if not rows:
            QMessageBox.information(
                self, "定位网格资产", "请先在结果树中选择 Actor 或一条/多条问题。"
            )
            return
        try:
            count = mesh_quality.sync_mesh_quality_assets(rows)
            self._mesh_quality_message(
                "已在 Content Browser 定位 {} 个 Static Mesh。".format(count)
            )
        except Exception as exc:
            self._mesh_quality_message("定位网格失败：{}".format(exc), True)
            QMessageBox.warning(self, "定位网格资产", str(exc))

    def _mesh_quality_item_double_clicked(self, item, _column: int):
        if item.data(0, _MESH_QUALITY_ITEM_KIND_ROLE) != "issue":
            return
        record = item.data(0, _MESH_QUALITY_RECORD_ROLE)
        if not record:
            return
        try:
            result = mesh_quality.locate_mesh_quality_row(
                record,
                focus_viewport=True,
                sync_content_browser=True,
            )
            self._mesh_quality_message(
                "已定位 {} 个 Actor 和 {} 个网格资产。".format(
                    result["actor_count"], result["asset_count"]
                )
            )
        except Exception as exc:
            self._mesh_quality_message("定位结果失败：{}".format(exc), True)

    def export_mesh_quality_report(self):
        if self._mesh_quality_scanned_groups <= 0:
            QMessageBox.information(self, "导出模型质量报告", "请先执行模型质量扫描。")
            return
        try:
            json_path, csv_path = mesh_quality.export_mesh_quality_report(
                self.mesh_quality_rows,
                scan_summary=self._mesh_quality_scan_summary,
            )
            self._mesh_quality_message(
                "报告已生成：\n{}\n{}".format(json_path, csv_path)
            )
        except Exception as exc:
            self._mesh_quality_message("导出报告失败：{}".format(exc), True)
            QMessageBox.critical(self, "导出模型质量报告", str(exc))

    def _unregister_mesh_quality_preview_tick(self):
        handle = self._mesh_quality_preview_handle
        if handle is None:
            return
        try:
            unreal.unregister_slate_post_tick_callback(handle)
            self._mesh_quality_preview_handle = None
        except Exception as exc:
            unreal.log_warning(
                "[MeshQuality] 注销视口标记回调失败（通常可忽略）：{}".format(exc)
            )

    def clear_mesh_quality_preview(self, *_args, log_message: bool = True):
        self._unregister_mesh_quality_preview_tick()
        state = self._mesh_quality_preview_state or {}
        entries = list(state.get("entries") or [])
        if entries:
            library = getattr(unreal, "DatasmithMeshAuditLibrary", None)
            clear_highlights = getattr(
                library, "clear_triangle_face_highlights", None
            )
            if callable(clear_highlights):
                try:
                    clear_highlights(entries[0]["component"])
                except Exception as exc:
                    unreal.log_warning(
                        "[MeshQuality] 清除错误区域高亮失败（短时高亮仍会自行消失）：{}".format(
                            exc
                        )
                    )
        self._mesh_quality_preview_state = None
        if log_message:
            self._mesh_quality_message("已清除视口中的红色错误区域高亮。")

    def _components_for_mesh_quality_row(self, row):
        wanted_components = set(getattr(row, "component_paths", None) or [])
        component_path = str(getattr(row, "component_path", "") or "")
        if component_path:
            wanted_components.add(component_path)
        wanted_actors = set(getattr(row, "actor_paths", None) or [])
        actor_path = str(getattr(row, "actor_path", "") or "")
        if actor_path:
            wanted_actors.add(actor_path)
        actor_subsystem = unreal.get_editor_subsystem(unreal.EditorActorSubsystem)
        matches = []
        seen_paths = set()
        for actor in actor_subsystem.get_all_level_actors():
            try:
                actor_path = str(actor.get_path_name())
            except Exception:
                continue
            if actor_path not in wanted_actors:
                continue
            try:
                components = actor.get_components_by_class(unreal.StaticMeshComponent)
            except Exception:
                components = []
            for component in components:
                try:
                    component_path = str(component.get_path_name())
                    if (
                        component_path in wanted_components
                        and component_path not in seen_paths
                    ):
                        matches.append(component)
                        seen_paths.add(component_path)
                except Exception:
                    pass
        return matches

    def _component_for_mesh_quality_row(self, row):
        """Compatibility singular accessor; new highlighting uses every match."""

        components = self._components_for_mesh_quality_row(row)
        return components[0] if components else None

    @staticmethod
    def _mesh_quality_xyz(value):
        if isinstance(value, (list, tuple)) and len(value) >= 3:
            return float(value[0]), float(value[1]), float(value[2])
        return float(value.x), float(value.y), float(value.z)

    @staticmethod
    def _mesh_quality_apply_transform(transform, local):
        if transform is None:
            return None
        for method_name in ("transform_location", "transform_position"):
            method = getattr(transform, method_name, None)
            if callable(method):
                try:
                    return method(local)
                except Exception:
                    pass
        math_library = getattr(unreal, "MathLibrary", None)
        method = getattr(math_library, "transform_location", None)
        if callable(method):
            try:
                return method(transform, local)
            except Exception:
                pass
        return None

    @staticmethod
    def _mesh_quality_component_world_transform(component):
        """Return component-to-world Transform across UE Python API variants."""

        failures = []
        # UE 5.6 exposes get_world_transform on SceneComponent, while some
        # older generated bindings exposed one of the component-named forms.
        for method_name in (
            "get_world_transform",
            "get_component_to_world",
            "get_component_transform",
        ):
            method = getattr(component, method_name, None)
            if not callable(method):
                continue
            try:
                transform = method()
                if transform is not None:
                    return transform, failures
            except Exception as exc:
                failures.append("{}: {}".format(method_name, exc))

        getter = getattr(component, "get_editor_property", None)
        if callable(getter):
            for property_name in ("component_to_world", "component_transform"):
                try:
                    transform = getter(property_name)
                    if transform is not None:
                        return transform, failures
                except Exception as exc:
                    failures.append("{}: {}".format(property_name, exc))

        # GetSocketTransform explicitly returns the component world transform
        # when the socket name does not exist, so NAME_None is a safe fallback.
        socket_method = getattr(component, "get_socket_transform", None)
        if callable(socket_method):
            name_type = getattr(unreal, "Name", None)
            try:
                socket_name = name_type("None") if callable(name_type) else "None"
            except Exception:
                socket_name = "None"
            argument_sets = [(socket_name,)]
            transform_space = getattr(unreal, "RelativeTransformSpace", None)
            world_space = getattr(transform_space, "RTS_WORLD", None)
            if world_space is not None:
                argument_sets.insert(0, (socket_name, world_space))
            for arguments in argument_sets:
                try:
                    transform = socket_method(*arguments)
                    if transform is not None:
                        return transform, failures
                except Exception as exc:
                    failures.append("get_socket_transform: {}".format(exc))

        # An Actor transform is equivalent only when this is the root
        # component.  Never use it for an offset child component.
        try:
            owner = component.get_owner()
            root = owner.get_root_component()
            if root == component:
                transform = owner.get_actor_transform()
                if transform is not None:
                    return transform, failures
        except Exception as exc:
            failures.append("root actor transform: {}".format(exc))
        return None, failures

    @classmethod
    def _mesh_quality_transform_point_from_world_trs(cls, component, local):
        """Last-resort world TRS transform for stripped generated bindings."""

        location = component.get_world_location()
        rotation = component.get_world_rotation()
        scale = None
        for method_name in (
            "get_world_scale",
            "get_component_scale",
            "get_world_scale3d",
        ):
            method = getattr(component, method_name, None)
            if callable(method):
                try:
                    scale = method()
                    break
                except Exception:
                    pass
        if scale is None:
            raise RuntimeError("组件未暴露世界缩放")

        local_xyz = cls._mesh_quality_xyz(local)
        scale_xyz = cls._mesh_quality_xyz(scale)
        scaled = unreal.Vector(
            local_xyz[0] * scale_xyz[0],
            local_xyz[1] * scale_xyz[1],
            local_xyz[2] * scale_xyz[2],
        )
        rotated = None
        rotate_vector = getattr(rotation, "rotate_vector", None)
        if callable(rotate_vector):
            try:
                rotated = rotate_vector(scaled)
            except Exception:
                pass
        if rotated is None:
            quaternion_method = getattr(rotation, "quaternion", None)
            if callable(quaternion_method):
                try:
                    quaternion = quaternion_method()
                    rotate_vector = getattr(quaternion, "rotate_vector", None)
                    if callable(rotate_vector):
                        rotated = rotate_vector(scaled)
                except Exception:
                    pass
        if rotated is None:
            # Match UE's FRotationMatrix axes: Yaw(Z) * Pitch(Y) * Roll(X).
            pitch = math.radians(float(rotation.pitch))
            yaw = math.radians(float(rotation.yaw))
            roll = math.radians(float(rotation.roll))
            sp, cp = math.sin(pitch), math.cos(pitch)
            sy, cy = math.sin(yaw), math.cos(yaw)
            sr, cr = math.sin(roll), math.cos(roll)
            x_axis = (cp * cy, cp * sy, sp)
            y_axis = (
                sr * sp * cy - cr * sy,
                sr * sp * sy + cr * cy,
                -sr * cp,
            )
            z_axis = (
                -(cr * sp * cy + sr * sy),
                cy * sr - cr * sp * sy,
                cr * cp,
            )
            scaled_xyz = cls._mesh_quality_xyz(scaled)
            rotated = unreal.Vector(
                scaled_xyz[0] * x_axis[0]
                + scaled_xyz[1] * y_axis[0]
                + scaled_xyz[2] * z_axis[0],
                scaled_xyz[0] * x_axis[1]
                + scaled_xyz[1] * y_axis[1]
                + scaled_xyz[2] * z_axis[1],
                scaled_xyz[0] * x_axis[2]
                + scaled_xyz[1] * y_axis[2]
                + scaled_xyz[2] * z_axis[2],
            )
        rotated_xyz = cls._mesh_quality_xyz(rotated)
        location_xyz = cls._mesh_quality_xyz(location)
        return unreal.Vector(
            rotated_xyz[0] + location_xyz[0],
            rotated_xyz[1] + location_xyz[1],
            rotated_xyz[2] + location_xyz[2],
        )

    @classmethod
    def _transform_mesh_quality_point(cls, component, values):
        local = unreal.Vector(float(values[0]), float(values[1]), float(values[2]))
        transform, failures = cls._mesh_quality_component_world_transform(component)
        transformed = cls._mesh_quality_apply_transform(transform, local)
        if transformed is not None:
            return transformed
        if transform is not None:
            failures.append("Transform 未暴露 transform_location/transform_position")
        try:
            return cls._mesh_quality_transform_point_from_world_trs(component, local)
        except Exception as exc:
            failures.append("world TRS: {}".format(exc))
        detail = "；".join(failures[-5:]) if failures else "没有可用的世界变换接口"
        raise RuntimeError(
            "当前 UE Python 绑定不能将局部采样点转换到世界坐标：{}".format(detail)
        )

    @staticmethod
    def _tuple_from_unreal_vector(value):
        return float(value.x), float(value.y), float(value.z)

    def _draw_mesh_quality_preview(self):
        state = self._mesh_quality_preview_state
        if not state:
            return 0
        entries = list(state.get("entries") or [])
        if not entries:
            return 0
        library = getattr(unreal, "DatasmithMeshAuditLibrary", None)
        draw_sample_highlights = getattr(
            library, "draw_triangle_face_highlights", None
        )
        draw_region_highlights = getattr(
            library, "draw_static_mesh_triangle_highlights", None
        )
        clear_highlights = getattr(library, "clear_triangle_face_highlights", None)
        needs_samples = any(entry.get("mode") == "samples" for entry in entries)
        needs_regions = any(entry.get("mode") == "triangle_ids" for entry in entries)
        if (
            not callable(clear_highlights)
            or (needs_samples and not callable(draw_sample_highlights))
            or (needs_regions and not callable(draw_region_highlights))
        ):
            missing = []
            if not callable(clear_highlights):
                missing.append("clear_triangle_face_highlights")
            if needs_samples and not callable(draw_sample_highlights):
                missing.append("draw_triangle_face_highlights")
            if needs_regions and not callable(draw_region_highlights):
                missing.append("draw_static_mesh_triangle_highlights")
            raise RuntimeError(
                "当前原生 DLL 与 {} Python 不兼容，缺少 {}。"
                "请完整更新插件并重启 UE；完整错误区域不会静默退回到"
                "截断的兼容预览。".format(
                    version.PLUGIN_VERSION,
                    "、".join(missing),
                )
            )

        red = unreal.LinearColor(1.0, 0.02, 0.02, 1.0)
        initial_draw = not bool(state.get("initial_draw_complete"))
        if initial_draw:
            clear_highlights(entries[0]["component"])
        drawn_faces = 0
        for entry in entries:
            if entry.get("mode") == "triangle_ids":
                # Complete Source LOD0 regions are submitted once as persistent
                # batched faces.  Rebuilding up to thousands of faces on every
                # Slate tick would recreate the UI stalls this path is meant to
                # avoid; clear_mesh_quality_preview removes the batch at expiry.
                if initial_draw:
                    triangle_ids = [int(value) for value in entry["triangle_ids"]]
                    drawn_faces += int(
                        draw_region_highlights(
                            entry["component"],
                            triangle_ids,
                            red,
                            -1.0,
                            True,
                            False,
                            int(entry.get("max_faces") or len(triangle_ids)),
                        )
                        or 0
                    )
                continue

            local_points = [
                unreal.Vector(float(value[0]), float(value[1]), float(value[2]))
                for value in entry["local_points"]
            ]
            drawn_faces += int(
                draw_sample_highlights(
                    entry["component"],
                    local_points,
                    red,
                    0.35,
                    True,
                    False,
                )
                or 0
            )
        if initial_draw:
            state["initial_draw_complete"] = True
        return drawn_faces

    def _mesh_quality_preview_tick(self, delta_seconds=0.0):
        state = self._mesh_quality_preview_state
        if not state:
            return
        state["remaining"] = state.get("remaining", 30.0) - float(
            delta_seconds or 0.0
        )
        if state["remaining"] <= 0.0:
            self.clear_mesh_quality_preview(log_message=False)
            return
        state["elapsed"] = state.get("elapsed", 0.0) + float(delta_seconds or 0.0)
        if state["elapsed"] < 0.12:
            return
        state["elapsed"] = 0.0
        try:
            self._draw_mesh_quality_preview()
        except Exception as exc:
            self.clear_mesh_quality_preview(log_message=False)
            self._mesh_quality_message("绘制视口标记失败：{}".format(exc), True)

    def preview_next_mesh_quality_sample(self):
        candidates = self._selected_mesh_quality_rows()
        if not candidates:
            QMessageBox.information(
                self,
                "红色线框高亮错误区域",
                "请先在结果树中选择一条问题；选择 Actor 行可高亮该 Actor 下全部可定位区域。",
            )
            return

        # Match the native guard.  Ordinary single-region clicks therefore
        # submit the complete Source LOD0 region; only genuinely extreme
        # multi-selection requests are truncated.
        max_faces = 100000
        requested_faces = 0
        remaining_faces = max_faces
        grouped_entries = {}
        drawable_rows = []
        missing_components = 0
        complete_region_faces = 0
        truncated_region_count = 0
        sample_fallback_regions = 0
        for row in candidates:
            components = self._components_for_mesh_quality_row(row)
            expected_components = max(1, len(row.component_paths))
            missing_components += max(0, expected_components - len(components))
            if not components:
                continue

            complete_count, _sample_count, region_truncated = (
                self._mesh_quality_region_stats(row)
            )
            complete_region_faces += complete_count * len(components)
            full_triangle_ids = []
            if (
                bool(row.details.get("native_region_fields", False))
                and str(row.triangle_id_space or "")
                == "source_mesh_description_lod0"
            ):
                full_triangle_ids = list(dict.fromkeys(
                    int(value) for value in row.error_triangle_ids
                ))
            available_sample_faces = len(row.sample_vertices) // 3
            if not full_triangle_ids and available_sample_faces <= 0:
                continue

            if full_triangle_ids:
                requested_faces += len(full_triangle_ids) * len(components)
            else:
                requested_faces += available_sample_faces * len(components)
                truncated_region_count += int(region_truncated)
                sample_fallback_regions += 1

            row_drawn = False
            for component in components:
                if remaining_faces <= 0:
                    break
                try:
                    component_key = str(component.get_path_name())
                except Exception:
                    component_key = str(id(component))

                if full_triangle_ids:
                    entry_key = (component_key, "triangle_ids")
                    entry = grouped_entries.setdefault(
                        entry_key,
                        {
                            "component": component,
                            "mode": "triangle_ids",
                            "triangle_ids": [],
                            "triangle_id_set": set(),
                        },
                    )
                    for triangle_id in full_triangle_ids:
                        if triangle_id in entry["triangle_id_set"]:
                            continue
                        if remaining_faces <= 0:
                            break
                        entry["triangle_id_set"].add(triangle_id)
                        entry["triangle_ids"].append(triangle_id)
                        remaining_faces -= 1
                        row_drawn = True
                    entry["max_faces"] = len(entry["triangle_ids"])
                else:
                    face_count = min(available_sample_faces, remaining_faces)
                    if face_count <= 0:
                        continue
                    entry_key = (component_key, "samples")
                    entry = grouped_entries.setdefault(
                        entry_key,
                        {
                            "component": component,
                            "mode": "samples",
                            "local_points": [],
                        },
                    )
                    entry["local_points"].extend(
                        list(row.sample_vertices[: face_count * 3])
                    )
                    remaining_faces -= face_count
                    row_drawn = True
            if row_drawn:
                drawable_rows.append(row)

        entries = list(grouped_entries.values())
        highlighted_faces = sum(
            len(item.get("triangle_ids") or [])
            if item.get("mode") == "triangle_ids"
            else len(item.get("local_points") or []) // 3
            for item in entries
        )
        if not entries or highlighted_faces <= 0:
            QMessageBox.warning(
                self,
                "红色线框高亮错误区域",
                "所选结果没有可绘制的 Source LOD0 错误三角面。Actor 负缩放等整实例问题只支持 Actor/资产定位；组件改变后请重新扫描。",
            )
            return

        self.clear_mesh_quality_preview(log_message=False)
        self._mesh_quality_preview_state = {
            "entries": entries,
            "elapsed": 0.0,
            "remaining": 60.0,
            "initial_draw_complete": False,
        }
        try:
            mesh_quality.select_mesh_quality_row_actors(
                drawable_rows, focus_viewport=True
            )
            actually_drawn = self._draw_mesh_quality_preview()
            if actually_drawn <= 0:
                raise RuntimeError("原生高亮接口没有接受任何有效三角面。")
            if self._mesh_quality_preview_handle is None:
                self._mesh_quality_preview_handle = (
                    unreal.register_slate_post_tick_callback(
                        self._mesh_quality_preview_tick
                    )
                )
            capped = requested_faces > max_faces
            suffix = (
                "；为避免视口卡顿，最多显示前 {} 个组件实例面".format(max_faces)
                if capped
                else ""
            )
            if truncated_region_count:
                suffix += "；{} 个兼容结果只能使用原生保留的预览面".format(
                    truncated_region_count
                )
            if sample_fallback_regions:
                suffix += "；{} 个旧格式/非 Source LOD0 结果使用兼容预览回退".format(
                    sample_fallback_regions
                )
            if missing_components:
                suffix += "；{} 条结果的场景组件已变化，未绘制".format(
                    missing_components
                )
            self._mesh_quality_message(
                "已用红色线框/半透明面高亮 {} 个三角面（所选区域/实例共记录 {} 个完整错误面），"
                "完整 Source LOD0 区域优先，持续 60 秒{}。".format(
                    actually_drawn, complete_region_faces, suffix
                )
            )
            if capped:
                QMessageBox.warning(
                    self,
                    "高亮数量已受安全上限限制",
                    "所选结果共请求 {} 个组件实例面，超过当前安全上限 {}。\n"
                    "已只高亮前 {} 个；请缩小到一个 Actor 或一个错误区域后重试，"
                    "避免编辑器因一次提交过多调试面而卡顿。".format(
                        requested_faces,
                        max_faces,
                        highlighted_faces,
                    ),
                )
        except Exception as exc:
            self.clear_mesh_quality_preview(log_message=False)
            self._mesh_quality_message("红色线框高亮错误区域失败：{}".format(exc), True)
            QMessageBox.warning(self, "红色线框高亮错误区域", str(exc))

    def _capture_scan_scope(self):
        scope = self._run("锁定扫描范围", core.capture_selected_scan_roots)
        if scope is None:
            return False
        self.scan_root_paths = [item["path"] for item in scope]
        self.scan_root_labels = [item["label"] for item in scope]
        shown = "、".join(self.scan_root_labels[:4])
        if len(self.scan_root_labels) > 4:
            shown += " 等 {} 个".format(len(self.scan_root_labels))
        self.scan_scope_label.setText(
            "已锁定 {} 个 Root：{}".format(len(self.scan_root_paths), shown)
        )
        self.scan_scope_label.setToolTip("\n".join(self.scan_root_paths))
        self._message(
            "扫描范围已锁定：{}。后续筛选 Actor 不会改变此范围。".format(shown)
        )
        return True

    def _ensure_scan_scope(self) -> bool:
        if self.scan_root_paths:
            return True
        return bool(self._capture_scan_scope())

    def _begin_scan(self, title: str):
        self._scan_active = True
        self._scan_cancel_requested = False
        self._scan_progress_title = title
        self.tabs.setEnabled(False)
        self.capture_scope_button.setEnabled(False)
        self.cancel_scan_button.setEnabled(True)
        self.cancel_scan_button.setText("取消当前任务")
        self.scan_progress_label.setText("{}：准备中".format(title))
        QApplication.processEvents()

    def _scan_progress(self, done: int, total: int) -> bool:
        title = getattr(self, "_scan_progress_title", "处理中")
        self.scan_progress_label.setText("{}：{} / {}".format(title, done, total))
        QApplication.processEvents()
        return not self._scan_cancel_requested

    def _request_scan_cancel(self):
        if not self._scan_active:
            return
        self._scan_cancel_requested = True
        self.cancel_scan_button.setEnabled(False)
        self.cancel_scan_button.setText("正在取消…")
        self.scan_progress_label.setText("正在安全停止当前任务…")

    def _end_scan(self):
        self._scan_active = False
        self.tabs.setEnabled(True)
        self.capture_scope_button.setEnabled(True)
        self.cancel_scan_button.setEnabled(False)
        self.cancel_scan_button.setText("取消当前任务")
        if self._scan_cancel_requested:
            self.scan_progress_label.setText("已取消")
        else:
            self.scan_progress_label.setText("空闲")
        QApplication.processEvents()

    def _run(self, title, callback):
        try:
            return callback()
        except core.ScanCancelledError as exc:
            self._message("{}：{}".format(title, exc))
            return None
        except Exception as exc:
            self._message("{}失败：{}".format(title, exc), True)
            unreal.log_error(traceback.format_exc())
            QMessageBox.critical(self, title, str(exc))
            return None

    def _pick_selected_asset(self, target: QLineEdit):
        try:
            assets = unreal.EditorUtilityLibrary.get_selected_assets()
            if not assets:
                raise RuntimeError("请先在 Content Browser 中选择一个材质。")
            material = assets[0]
            if not core.load_material(material.get_path_name()):
                raise RuntimeError("当前选择不是材质或材质实例。")
            target.setText(material.get_path_name())
            self._save_state()
        except Exception as exc:
            QMessageBox.warning(self, "选择材质", str(exc))

    def _pick_selected_function(self):
        try:
            assets = unreal.EditorUtilityLibrary.get_selected_assets()
            if not assets:
                raise RuntimeError("请先在 Content Browser 中选择一个 Material Function。")
            function = assets[0]
            if not core.load_material_function(function.get_path_name()):
                raise RuntimeError(
                    "当前选择不是 Material Function 或 Material Function Instance。"
                )
            self.normal_function_path.setText(function.get_path_name())
            self._save_state()
        except Exception as exc:
            QMessageBox.warning(self, "选择 Normal 补全函数", str(exc))

    def scan_uv(self):
        self._save_state()
        self._begin_scan("UV")
        try:
            result = self._run(
                "扫描 UV 网格",
                lambda: core.scan_uv_assets(
                    large_projection_size=self.uv_large_size.value(),
                    small_projection_size=self.uv_small_size.value(),
                    large_area_threshold_m2=self.uv_area_threshold.value(),
                    root_paths=None,
                    progress_callback=self._scan_progress,
                ),
            )
            if result is not None:
                self.uv_records = result
                self.scan_progress_label.setText("正在生成 UV 表格…")
                QApplication.processEvents()
                self._populate_uv_table()
        finally:
            self._end_scan()
        if result is None:
            return
        self._message(
            "UV 扫描完成：{} 个唯一网格/缩放组。".format(len(self.uv_records))
        )

    @staticmethod
    def _format_dimensions(values, decimals=1):
        return " × ".join(
            ("{:,.%df}" % decimals).format(float(value)) for value in values
        )

    @staticmethod
    def _uv_classification_label(value: str) -> str:
        return {
            "large": "大型",
            "small": "小型",
            "skipped": "跳过",
        }.get(value, value)

    def _populate_uv_table(self):
        green = QColor("#74d680")
        cyan = QColor("#67c7eb")
        orange = QColor("#ffbf69")
        sorting_enabled = self.uv_table.isSortingEnabled()
        self.uv_table.setSortingEnabled(False)
        self.uv_table.blockSignals(True)
        self.uv_table.setUpdatesEnabled(False)
        try:
            self.uv_table.clearContents()
            self.uv_table.setRowCount(len(self.uv_records))
            for row, record in enumerate(self.uv_records):
                class_color = {
                    "large": cyan,
                    "small": green,
                    "skipped": orange,
                }.get(record.classification)
                values = [
                    self._readonly_item(record.mesh_name),
                    self._readonly_item(" | ".join(record.actor_labels)),
                    self._readonly_item(str(record.component_count)),
                    self._readonly_item(self._format_dimensions(record.local_dimensions)),
                    self._readonly_item(self._format_dimensions(record.world_dimensions)),
                    self._readonly_item(self._format_dimensions(record.world_scale, 3)),
                    self._readonly_item("{:.2f}".format(record.major_area_m2)),
                    self._readonly_item(
                        self._uv_classification_label(record.classification), class_color
                    ),
                    self._readonly_item(
                        "{:.1f} × {:.1f} × {:.1f} cm".format(
                            record.projection_size,
                            record.projection_size,
                            record.projection_size,
                        )
                        if record.projection_size > 0
                        else "—"
                    ),
                    self._readonly_item(record.reason),
                ]
                values[0].setToolTip(record.mesh_path)
                values[1].setToolTip(
                    "\n".join(
                        "{} / {}".format(
                            item.get("actor_path", ""), item.get("component_name", "")
                        )
                        for item in record.components
                    )
                )
                for column, item in enumerate(values):
                    self.uv_table.setItem(row, column, item)
        finally:
            self.uv_table.blockSignals(False)
            self.uv_table.setSortingEnabled(sorting_enabled)
            self.uv_table.setUpdatesEnabled(True)
            self.uv_table.viewport().update()
        self._update_uv_summary()
        self._apply_uv_filter()

    def _update_uv_summary(self):
        counts = {"large": 0, "small": 0, "skipped": 0}
        components = 0
        unique_meshes = set()
        for record in self.uv_records:
            counts[record.classification] = counts.get(record.classification, 0) + 1
            components += record.component_count
            unique_meshes.add(record.mesh_path)
        self.uv_summary.setText(
            "缩放组 {groups}｜唯一源网格 {meshes}｜组件实例 {components}｜"
            "大型 {large}｜小型 {small}｜跳过 {skipped}".format(
                groups=len(self.uv_records),
                meshes=len(unique_meshes),
                components=components,
                **counts
            )
        )

    def _apply_uv_filter(self):
        selected_class = str(self.uv_filter_combo.currentData() or "all")
        self.uv_table.setUpdatesEnabled(False)
        try:
            for row, record in enumerate(self.uv_records):
                visible = (
                    selected_class == "all" or record.classification == selected_class
                )
                self.uv_table.setRowHidden(row, not visible)
        finally:
            self.uv_table.setUpdatesEnabled(True)
            self.uv_table.viewport().update()

    def _visible_uv_records(self):
        return [
            record
            for row, record in enumerate(self.uv_records)
            if not self.uv_table.isRowHidden(row)
        ]

    def _selected_uv_rows(self):
        selection_model = self.uv_table.selectionModel()
        if not selection_model:
            return []
        return sorted(index.row() for index in selection_model.selectedRows())

    def _mark_selected_uv(self, classification: str):
        rows = self._selected_uv_rows()
        if not rows:
            QMessageBox.information(self, "调整 UV 分类", "请先在表格中选择一行或多行。")
            return
        for row in rows:
            record = self.uv_records[row]
            record.classification = classification
            if classification == "large":
                record.projection_size = self.uv_large_size.value()
                record.reason = "人工标记为大型"
            elif classification == "small":
                record.projection_size = self.uv_small_size.value()
                record.reason = "人工标记为小型"
            else:
                record.projection_size = 0.0
                record.reason = "人工标记为跳过"
        self._populate_uv_table()

    def select_filtered_uv_actors(self):
        visible_records = self._visible_uv_records()
        if not visible_records:
            QMessageBox.information(self, "筛选 UV 物品", "当前筛选结果为空。")
            return
        selected = self._run(
            "筛选 UV 物品", lambda: core.select_uv_record_actors(visible_records)
        )
        if selected is not None:
            self._message("已在 World Outliner 中选中 {} 个 UV 筛选物品。".format(selected))

    def apply_uv(self):
        if not self.uv_records:
            QMessageBox.information(self, "盒体 UV", "请先扫描并检查自动分类。")
            return
        large = sum(record.classification == "large" for record in self.uv_records)
        small = sum(record.classification == "small" for record in self.uv_records)
        components = sum(
            record.component_count
            for record in self.uv_records
            if record.classification in ("large", "small")
        )
        message = (
            "将处理大型缩放组 {large} 个、小型缩放组 {small} 个，共影响 {components} 个组件。\n"
            "将直接覆盖原 Static Mesh 的 UV{channel}，不生成备份且无法自动撤销。\n"
            "处理中不会逐个保存；所有成功修改的网格会在任务结束或取消时统一保存一次。是否继续？"
        ).format(
            large=large,
            small=small,
            components=components,
            channel=int(self.uv_channel.currentData() or 0),
        )
        if QMessageBox.question(self, "确认盒体 UV", message) != QMessageBox.Yes:
            return
        self._begin_scan("盒体 UV")
        try:
            result = self._run(
                "盒体 UV",
                lambda: core.apply_uv_box_projection(
                    self.uv_records,
                    int(self.uv_channel.currentData() or 0),
                    progress_callback=self._scan_progress,
                ),
            )
        finally:
            self._end_scan()
        if result:
            status = "；任务已取消，已保存完成部分" if result["cancelled"] else ""
            self._message(
                "UV 处理完成：直接修改原网格 {processed_meshes} 个，"
                "影响组件 {affected_components} 个，缩放冲突 {conflicts} 个，"
                "批量保存 {save_batches} 次，错误 {errors}{status}。".format(
                    processed_meshes=result["processed_meshes"],
                    affected_components=result["affected_components"],
                    conflicts=result["conflicts"],
                    save_batches=result["save_batches"],
                    errors=len(result["errors"]),
                    status=status,
                )
            )
            for error in result["errors"]:
                self._message(error, True)

    def export_uv_report(self):
        if not self.uv_records:
            QMessageBox.information(self, "导出 UV 报告", "请先扫描 UV 网格。")
            return
        result = self._run(
            "导出 UV 报告", lambda: core.export_uv_report(self.uv_records)
        )
        if result:
            self._message("UV 报告已生成：\n{}\n{}".format(result[0], result[1]))

    def scan(self):
        if not self._ensure_scan_scope():
            return
        self._begin_scan("材质")
        try:
            result = self._run(
                "自动扫描",
                lambda: core.scan_selected_roots(
                    _default_rules_file(),
                    root_paths=self.scan_root_paths,
                    progress_callback=self._scan_progress,
                ),
            )
            if result is not None:
                self.records = result
                self.scan_progress_label.setText("正在生成材质表格…")
                QApplication.processEvents()
                self._populate_table()
        finally:
            self._end_scan()
        if result is None:
            return
        self._message("自动扫描完成：{} 个材质槽。".format(len(self.records)))

    def _readonly_item(self, text: str, color: QColor | None = None):
        item = QTableWidgetItem(text)
        if color:
            item.setForeground(color)
        return item

    def _build_material_rows(self):
        """Aggregate the visible table by unique material path.

        All slot records remain in ``self.records`` for processing and actor
        selection. Only the presentation is compacted, so a scene with tens of
        thousands of repeated slots does not create hundreds of thousands of
        Qt item objects.
        """
        groups = {}
        for record in self.records:
            group = groups.get(record.material_path)
            if group is None:
                group = {
                    "record": record,
                    "slot_count": 0,
                    "actor_labels": set(),
                    "slot_names": set(),
                }
                groups[record.material_path] = group
            group["slot_count"] += 1
            group["actor_labels"].add(record.actor_label)
            group["slot_names"].add(record.slot_name)
        self.material_rows = sorted(
            groups.values(),
            key=lambda group: group["record"].material_name.lower(),
        )

    @staticmethod
    def _limited_tooltip(values, limit=30):
        ordered = sorted(str(value) for value in values if value)
        shown = ordered[:limit]
        if len(ordered) > limit:
            shown.append("……另有 {} 项".format(len(ordered) - limit))
        return "\n".join(shown)

    def _populate_table(self):
        green = QColor("#74d680")
        red = QColor("#ff7070")
        orange = QColor("#ffbf69")
        cyan = QColor("#67c7eb")
        self._build_material_rows()
        sorting_enabled = self.table.isSortingEnabled()
        self.table.setSortingEnabled(False)
        self.table.blockSignals(True)
        self.table.setUpdatesEnabled(False)
        try:
            self.table.clearContents()
            self.table.setRowCount(len(self.material_rows))
            for row, group in enumerate(self.material_rows):
                record = group["record"]
                actor_count = len(group["actor_labels"])
                normal_color = green if record.writes_world_normal else red
                depth_color = green if record.writes_scene_depth else red
                normal_input_color = green if record.normal_connected else orange
                two_sided_color = green if record.two_sided else orange
                class_color = cyan if record.classification == "glass" else None
                values = [
                    self._readonly_item("{} 个 Actor".format(actor_count)),
                    self._readonly_item(str(group["slot_count"])),
                    self._readonly_item(record.material_name),
                    self._readonly_item(record.blend_mode),
                    self._readonly_item(record.shading_model),
                    self._readonly_item(_yes_no(record.writes_world_normal), normal_color),
                    self._readonly_item(_yes_no(record.writes_scene_depth), depth_color),
                    self._readonly_item(
                        "已连接" if record.normal_connected else "未连接",
                        normal_input_color,
                    ),
                    self._readonly_item(
                        "已开启" if record.two_sided else "未开启", two_sided_color
                    ),
                    self._readonly_item(
                        _classification_label(record.classification), class_color
                    ),
                    self._readonly_item(record.reason),
                ]
                values[0].setToolTip(self._limited_tooltip(group["actor_labels"]))
                values[1].setToolTip(self._limited_tooltip(group["slot_names"]))
                values[2].setToolTip(record.material_path)
                values[2].setData(Qt.ItemDataRole.UserRole, record.material_path)
                values[7].setToolTip("母材质：{}".format(record.base_material_path))
                values[8].setToolTip("母材质：{}".format(record.base_material_path))
                for column, item in enumerate(values):
                    self.table.setItem(row, column, item)
        finally:
            self.table.blockSignals(False)
            self.table.setSortingEnabled(sorting_enabled)
            self.table.setUpdatesEnabled(True)
            self.table.viewport().update()
        self._update_summary()
        self._apply_filter()

    def _current_filter(self) -> str:
        value = self.filter_combo.currentData()
        return str(value or "all")

    @staticmethod
    def _record_matches_filter(record, selected_class: str) -> bool:
        if selected_class == "normal_missing":
            return not record.normal_connected
        return selected_class == "all" or record.classification == selected_class

    def _visible_records(self):
        selected_class = self._current_filter()
        return [
            record
            for record in self.records
            if self._record_matches_filter(record, selected_class)
        ]

    def _apply_filter(self):
        selected_class = self._current_filter()
        self.table.setUpdatesEnabled(False)
        try:
            for row, group in enumerate(self.material_rows):
                visible = self._record_matches_filter(
                    group["record"], selected_class
                )
                self.table.setRowHidden(row, not visible)
        finally:
            self.table.setUpdatesEnabled(True)
            self.table.viewport().update()

    def select_filtered_actors(self):
        visible_records = self._visible_records()
        if not visible_records:
            QMessageBox.information(self, "筛选物品", "当前筛选结果为空。")
            return
        selected = self._run(
            "筛选物品", lambda: core.select_record_actors(visible_records)
        )
        if selected is not None:
            self._message("已在 World Outliner 中选中 {} 个筛选物品。".format(selected))

    def _selected_visible_material_paths(self):
        selection_model = self.table.selectionModel()
        if not selection_model:
            return []
        paths = []
        for index in selection_model.selectedRows():
            row = index.row()
            if self.table.isRowHidden(row):
                continue
            item = self.table.item(row, 2)
            path = item.data(Qt.ItemDataRole.UserRole) if item else ""
            if path:
                paths.append(str(path))
        return sorted(set(paths))

    def locate_filtered_materials(self):
        selected_paths = self._selected_visible_material_paths()
        if selected_paths:
            material_paths = selected_paths
            source = "表格选中行"
        else:
            material_paths = sorted(
                {
                    record.material_path
                    for record in self._visible_records()
                    if record.material_path
                }
            )
            source = "当前筛选"
        if not material_paths:
            QMessageBox.information(self, "定位筛选材质", "当前筛选结果中没有材质资产。")
            return
        result = self._run(
            "定位筛选材质",
            lambda: core.sync_content_browser_to_materials(material_paths),
        )
        if result is not None:
            self._message(
                "已在 Content Browser 中定位 {} 个{}唯一材质；无效路径 {} 个。".format(
                    result["located"], source, len(result["missing"])
                )
            )

    def _update_summary(self):
        counts = {"glass": 0, "non_glass": 0, "issue": 0, "excluded": 0}
        missing_two_sided_bases = set()
        missing_normal_bases = set()
        unique_materials = set()
        for record in self.records:
            counts[record.classification] = counts.get(record.classification, 0) + 1
            if not record.two_sided and record.base_material_path:
                missing_two_sided_bases.add(record.base_material_path)
            if (
                not record.normal_connected
                and record.classification != "excluded"
                and record.base_material_path
            ):
                missing_normal_bases.add(record.base_material_path)
            unique_materials.add(record.material_path)
        self.summary.setText(
            "槽位 {slots}｜唯一材质 {unique}｜自动玻璃 {glass}｜非玻璃 {non_glass}｜"
            "渲染异常 {issue}｜特殊域 {excluded}｜未开启双面的母材质 {two_sided}｜"
            "Normal 未连接母材质 {normal_missing}".format(
                slots=len(self.records),
                unique=len(unique_materials),
                two_sided=len(missing_two_sided_bases),
                normal_missing=len(missing_normal_bases),
                **counts
            )
        )

    def apply_processing(self):
        if not self.records:
            QMessageBox.information(self, "正式处理", "请先执行自动扫描。")
            return
        glass_count = sum(r.classification == "glass" for r in self.records)
        if glass_count and not core.load_material(self.glass_path.text()):
            QMessageBox.warning(self, "正式处理", "请先指定有效的替换用玻璃材质。")
            return
        issue_count = sum(r.classification == "issue" for r in self.records)
        message = (
            "自动检测到玻璃槽 {} 个。将替换玻璃，并把非玻璃标准化为 Opaque + Default Lit。\n"
            "渲染异常 {} 个会被跳过。双面自动处理：{}。是否继续？"
        ).format(
            glass_count,
            issue_count,
            "开启" if self.auto_two_sided.isChecked() else "关闭",
        )
        if QMessageBox.question(self, "确认正式处理", message) != QMessageBox.Yes:
            return
        self._save_state()
        result = self._run(
            "正式处理",
            lambda: core.apply_processing(
                self.records,
                self.glass_path.text(),
                enable_two_sided=self.auto_two_sided.isChecked(),
            ),
        )
        if result:
            self._message(
                "处理完成：实际改动 {changed}，玻璃槽 {glass}，非玻璃槽 {opaque}，"
                "开启母材质双面 {two_sided}/{unique_bases}，错误 {errors}。请检查后保存关卡。".format(
                    changed=result["changed"],
                    glass=result["glass"],
                    opaque=result["opaque"],
                    two_sided=result["two_sided"],
                    unique_bases=result["unique_bases"],
                    errors=len(result["errors"]),
                )
            )
            for error in result["errors"]:
                self._message(error, True)

    def enable_two_sided(self):
        if not self.records:
            QMessageBox.information(self, "开启双面", "请先执行自动扫描。")
            return
        missing_bases = {
            record.base_material_path
            for record in self.records
            if not record.two_sided and record.base_material_path
        }
        missing = len(missing_bases)
        if not missing:
            QMessageBox.information(self, "开启双面", "扫描范围内的材质均已开启双面。")
            return
        if QMessageBox.question(
            self,
            "开启双面",
            "检测到 {} 个未开启双面的唯一母材质。将直接修改、编译并保存这些母材质，是否继续？".format(missing),
        ) != QMessageBox.Yes:
            return
        result = self._run("开启双面", lambda: core.enable_two_sided(self.records))
        if result:
            self._message(
                "双面处理完成：检查母材质 {}，新开启 {}，原本已开启 {}，错误 {}。建议重新扫描。".format(
                    result["unique_bases"],
                    result["changed"],
                    result["already_enabled"],
                    len(result["errors"]),
                )
            )

    def connect_normal_function(self):
        if not self.records:
            QMessageBox.information(self, "补全 Normal", "请先执行自动扫描。")
            return
        material_function = core.load_material_function(
            self.normal_function_path.text()
        )
        if not material_function:
            QMessageBox.warning(
                self,
                "补全 Normal",
                "请先在 Content Browser 选择有效的 Material Function，并读取到面板。",
            )
            return
        missing_bases = {
            record.base_material_path
            for record in self.records
            if not record.normal_connected
            and record.classification != "excluded"
            and record.base_material_path
        }
        if not missing_bases:
            QMessageBox.information(
                self, "补全 Normal", "扫描范围内的表面母材质均已有 Normal 连接。"
            )
            return
        message = (
            "检测到 {} 个 Normal 未连接的唯一母材质。\n"
            "将把所选函数的第一个输出接到这些母材质的 Normal，并编译、保存母材质；"
            "已有 Normal 连接的材质不会被覆盖。是否继续？"
        ).format(len(missing_bases))
        if QMessageBox.question(self, "确认补全 Normal", message) != QMessageBox.Yes:
            return
        self._save_state()
        result = self._run(
            "补全 Normal",
            lambda: core.connect_missing_normal_function(
                self.records, self.normal_function_path.text()
            ),
        )
        if result:
            self._message(
                "Normal 补全完成：检查母材质 {}，新连接 {}，原本已连接 {}，错误 {}。"
                "请重新扫描查看结果。".format(
                    result["unique_bases"],
                    result["changed"],
                    result["already_connected"],
                    len(result["errors"]),
                )
            )
            for error in result["errors"]:
                self._message(error, True)

    def enter_world_normal(self):
        result = self._run("World Normal", core.enter_world_normal)
        if result:
            self._message(
                "已通过 Level Viewport API 切换到 World Normal（模式：{}，缓冲：{}）。".format(
                    result["view_mode"], result["buffer_target"]
                )
            )

    def enter_scene_depth(self):
        if not self.records:
            QMessageBox.information(self, "自定义 Scene Depth", "请先执行自动扫描。")
            return
        if not core.load_material(self.post_depth_path.text()):
            QMessageBox.warning(
                self,
                "自定义 Scene Depth",
                "请先指定有效的 Post Process 域材质或材质实例。",
            )
            return
        self._save_state()
        result = self._run(
            "自定义 Scene Depth",
            lambda: core.enter_scene_depth(
                self.records,
                self.post_depth_path.text(),
            ),
        )
        if result:
            self._message(
                "已将玻璃的唯一母材质临时改为 Opaque：改动 {}/{}，原本 Opaque {}；"
                "Level Viewport 保持 Lit，并用临时无限范围后期体积启用 {}；错误 {}。".format(
                    result["changed_bases"],
                    result["unique_bases"],
                    result["already_opaque"],
                    result["post_process"]["material_path"],
                    len(result["errors"]),
                )
            )

    def restore_debug(self):
        result = self._run("恢复检查状态", core.restore_debug_and_view)
        if result:
            self._message(
                "已移除 {} 个临时后期体积、恢复 {} 个玻璃母材质并返回 Lit；"
                "兼容恢复旧版材质槽 {} 个。".format(
                    result.get("restored_preview", 0),
                    result["restored_bases"],
                    result["restored_slots"],
                )
            )

    def restore_original(self):
        if QMessageBox.question(
            self,
            "撤销正式处理",
            "将恢复最近一次处理前的全部材质槽。是否继续？",
        ) != QMessageBox.Yes:
            return
        result = self._run("撤销正式处理", core.restore_original)
        if result:
            self._message(
                "已恢复 {} 个原始材质槽和 {} 个母材质双面设置。".format(
                    result["restored"], result.get("restored_bases", 0)
                )
            )

    def export_report(self):
        if not self.records:
            QMessageBox.information(self, "导出报告", "请先执行自动扫描。")
            return
        result = self._run("导出报告", lambda: core.export_report(self.records))
        if result:
            self._message("报告已生成：\n{}\n{}".format(result[0], result[1]))

    def closeEvent(self, event):
        # Never leave a Slate callback pointing at a closed/reloaded window.
        self._closing = True
        if self._scan_active:
            self._scan_cancel_requested = True
        if self._mesh_quality_job_state is not None:
            self._scan_cancel_requested = True
            self._finish_mesh_quality_scan(cancelled=True)
        else:
            self._unregister_mesh_quality_tick()
        self.clear_mesh_quality_preview(log_message=False)
        super().closeEvent(event)

def show_window():
    global _WINDOW, _QT_APP
    app = QApplication.instance()
    if not app:
        _QT_APP = QApplication(sys.argv if sys.argv else [])
    # importlib.reload keeps the old module dictionary. Replace a window made
    # from the previous class definition so menu-driven hot reloads are safe.
    if _WINDOW is not None and not isinstance(_WINDOW, MaterialAuditorWindow):
        try:
            _WINDOW.close()
            _WINDOW.deleteLater()
        except Exception:
            pass
        _WINDOW = None
    if _WINDOW is None:
        _WINDOW = MaterialAuditorWindow()
    _WINDOW._closing = False
    _WINDOW.showNormal()
    _WINDOW.raise_()
    _WINDOW.activateWindow()
    return _WINDOW
