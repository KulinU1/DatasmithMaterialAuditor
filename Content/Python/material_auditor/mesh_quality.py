# -*- coding: utf-8 -*-
"""Read-only orchestration for Static Mesh normal and UV diagnostics.

The expensive triangle inspection lives in the native editor module.  This
module deliberately owns only editor scope discovery, per-instance grouping,
result adaptation, navigation, and report export.  It never modifies or saves
a Static Mesh, component, actor, level, or package.

Native contract (UE Python names)::

    unreal.DatasmithMeshAuditOptions
    unreal.DatasmithMeshAuditLibrary.analyze_static_mesh(mesh, options)

Both the native result structs and Unreal enum string representations can vary
slightly between generated Python builds, so result reading is intentionally
tolerant while the public dataclasses below remain stable for the Qt UI.
"""

from __future__ import annotations

import csv
import io
import json
import math
import os
import tempfile
from collections import Counter
from dataclasses import asdict, dataclass, field, fields
from datetime import datetime
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

import unreal

from . import version


API_VERSION = version.MESH_QUALITY_API_VERSION
REPORT_SCHEMA_VERSION = version.REPORT_SCHEMA_VERSION
TOOL_NAME = "DatasmithMaterialAuditor"
SCAN_PROGRESS_INTERVAL = 25


class NativeAuditUnavailableError(RuntimeError):
    """Raised when the compiled mesh-audit editor module is not loaded."""


class MeshQualityScanCancelled(RuntimeError):
    """Raised when a progress callback cancels a scan."""


@dataclass(frozen=True)
class MeshComponentUse:
    """One level component represented by an audit group."""

    actor_path: str
    actor_label: str
    component_path: str
    component_name: str
    transform_determinant: float = 1.0
    orientation_reversing: bool = False


@dataclass
class MeshAuditGroup:
    """One unique Static Mesh + signed component world-scale combination."""

    mesh_path: str
    mesh_name: str
    world_scale: Tuple[float, float, float]
    uses: List[MeshComponentUse]
    mesh: Any = field(repr=False, compare=False)
    audit_result_summary: Dict[str, Any] = field(
        default_factory=dict, repr=False, compare=False
    )

    @property
    def component_count(self) -> int:
        return len(self.uses)


@dataclass
class MeshQualityOptions:
    """Stable Python settings mirrored to ``DatasmithMeshAuditOptions``."""

    uv_channel: int = 0
    max_sample_triangles: int = 128
    geometric_area_epsilon: float = 1.0e-8
    normal_zero_tolerance: float = 1.0e-4
    normal_unit_tolerance: float = 0.05
    report_non_unit_normals: bool = True
    # 0.9.5 deliberately fixes the artist-facing classification boundary to
    # the native contract: dot < -0.2 is reversed and abs(dot) <= 0.2 is only
    # a warning.  These mirrored values are still transferred and verified so
    # a stale DLL cannot silently run the old thresholds.
    normal_dot_warning_threshold: float = 0.2
    normal_dot_error_threshold: float = -0.2
    report_ambiguous_normals: bool = True
    # 0.9.5 artist-facing standard audit must actually run the conservative
    # topology/normal candidate pass.  The finding remains a review result;
    # Actor mirroring is emitted separately as a transform issue.
    report_normal_direction_mismatch: bool = True
    uv_area_epsilon: float = 1.0e-10
    uv_stretch_warning_threshold: float = 3.0
    uv_stretch_error_threshold: float = 6.0
    non_uniform_scale_tolerance: float = 0.001
    include_scale_issues: bool = True
    native_overrides: Dict[str, Any] = field(default_factory=dict)


@dataclass
class MeshQualityRow:
    """One UI table row, shared by native and component-transform issues."""

    severity: str
    category: str
    issue_code: str
    issue_type: str
    message: str
    mesh_path: str
    mesh_name: str
    world_scale: Tuple[float, float, float]
    actor_path: str
    actor_label: str
    component_path: str
    component_name: str
    actor_paths: List[str]
    actor_labels: List[str]
    component_paths: List[str]
    component_names: List[str]
    component_count: int
    detail_code: str = ""
    mesh_triangle_count: int = 0
    uv_channel_count: int = 0
    material_slot: int = -1
    affected_count: int = 0
    max_metric: float = 0.0
    lod_index: int = 0
    region_id: str = ""
    region_index: int = -1
    error_triangle_ids: List[int] = field(default_factory=list)
    triangle_id_space: str = ""
    local_bounds_min: Tuple[float, float, float] = (0.0, 0.0, 0.0)
    local_bounds_max: Tuple[float, float, float] = (0.0, 0.0, 0.0)
    samples_truncated: bool = False
    # Retained preview/sample IDs.  0.9.4 native results only expose this
    # field, so adapters continue to accept it as the fallback complete list.
    triangle_ids: List[int] = field(default_factory=list)
    sample_vertices: List[Tuple[float, float, float]] = field(default_factory=list)
    sample_face_normals: List[Tuple[float, float, float]] = field(default_factory=list)
    analysis_milliseconds: float = 0.0
    source: str = "native"
    details: Dict[str, Any] = field(default_factory=dict)


_NATIVE_OPTION_NAMES = (
    "uv_channel",
    "max_sample_triangles",
    "geometric_area_epsilon",
    "normal_zero_tolerance",
    "normal_unit_tolerance",
    "report_non_unit_normals",
    "normal_dot_warning_threshold",
    "normal_dot_error_threshold",
    "report_ambiguous_normals",
    "report_normal_direction_mismatch",
    "uv_area_epsilon",
    "uv_stretch_warning_threshold",
    "uv_stretch_error_threshold",
)

# Missing or ignored values here change what is actually audited.  They must
# never be silently skipped when Python and the compiled editor DLL differ.
_REQUIRED_NATIVE_OPTION_NAMES = frozenset(_NATIVE_OPTION_NAMES)

_RESULT_SUMMARY_FIELDS = (
    "success",
    "error",
    "mesh_path",
    "triangle_count",
    "uv_channel_count",
    "normal_data_source",
    "lod_index",
    "analysis_milliseconds",
    "closed_shell_direction_candidate_count",
    "closed_shell_direction_evaluated_count",
    "closed_shell_direction_abstained_count",
    "direction_abstention_reasons",
)

_REQUIRED_DIRECTION_SUMMARY_FIELDS = (
    "closed_shell_direction_candidate_count",
    "closed_shell_direction_evaluated_count",
    "closed_shell_direction_abstained_count",
    "direction_abstention_reasons",
)

_REPORT_COLUMNS = tuple(item.name for item in fields(MeshQualityRow))


# ``FDatasmithMeshAuditIssue.Count`` deliberately stores the number of source
# mesh elements hit by one aggregated result group.  The element type differs
# by issue code, so presenting it as a bare "异常数量" is misleading (for
# example, invalid/non-unit normals count triangle corners, visual direction
# checks count triangles, and topology checks count edges). Keep the serialized
# integer stable and expose the unit separately to the UI.
_AFFECTED_ELEMENT_UNITS = {
    "INVALID_NORMAL": "角点",
    "DEGENERATE_TRIANGLE": "三角面",
    "LOCAL_WINDING_CONFLICT": "三角面",
    "VERTEX_NORMAL_REVERSED": "三角面",
    "CLOSED_SHELL_INWARD": "三角面",
    "NON_MANIFOLD_TOPOLOGY": "边",
    "OPEN_MESH_DIRECTION_UNKNOWN": "三角面",
    "INVALID_VERTEX_NORMAL": "角点",
    "NON_UNIT_VERTEX_NORMAL": "角点",
    "REVERSED_VERTEX_NORMAL": "三角面",
    "AMBIGUOUS_VERTEX_NORMAL": "三角面",
    "DEGENERATE_GEOMETRY": "三角面",
    "MISSING_UV_CHANNEL": "三角面",
    "INVALID_TRIANGLE_UV": "三角面",
    "DEGENERATE_TRIANGLE_UV": "三角面",
    "UV_STRETCH": "三角面",
    "WINDING_CONFLICT": "边",
    "NON_MANIFOLD_EDGE": "边",
}

_NO_AFFECTED_ELEMENT_CODES = frozenset(
    {
        "ACTOR_NEGATIVE_SCALE",
        "NATIVE_ANALYSIS_FAILED",
    }
)

_ISSUE_MESSAGES_ZH = {
    "INVALID_NORMAL": "源 MeshDescription LOD0 法线包含 NaN、Infinity 或接近零的无效值。",
    "DEGENERATE_TRIANGLE": "三角面包含重复点、共线点、无效坐标或接近零面积。",
    "LOCAL_WINDING_CONFLICT": "相邻三角面沿共享边使用了冲突绕序，请复查该连续区域。",
    "VERTEX_NORMAL_REVERSED": (
        "源网格顶点实例法线与局部三角面方向相反；请结合红色错误区域高亮和 World Normal 复查。"
    ),
    "CLOSED_SHELL_INWARD": "闭合壳体的连续表面整体朝内，请复查壳体外侧方向。",
    "NON_MANIFOLD_TOPOLOGY": "连续区域包含一条连接三个或更多三角面的非流形边。",
    "OPEN_MESH_DIRECTION_UNKNOWN": (
        "该区域属于开放网格，无法仅凭拓扑确定外侧方向；此项只是提示，不是错误。"
    ),
    "ACTOR_NEGATIVE_SCALE": (
        "Actor/组件完整 ComponentToWorld 矩阵行列式小于零，实例发生镜像；"
        "源 Static Mesh 不会因此判为法线错误。"
    ),
    "DEGENERATE_GEOMETRY": "三角面包含重复点、共线点、无效坐标或接近零面积。",
    "INVALID_VERTEX_NORMAL": "UE 构建后的渲染法线仍然无效或接近零。",
    "NON_UNIT_VERTEX_NORMAL": "法线长度偏离 1；此项只在严格数据诊断中显示。",
    "REVERSED_VERTEX_NORMAL": (
        "CPU 顶点法线与该三角面的几何绕序方向相反；这只是严格模式复查候选，"
        "双面材质、负缩放、反向剔除、Nanite 或材质法线可能让最终 World Normal 完全正常。"
    ),
    "AMBIGUOUS_VERTEX_NORMAL": (
        "构建后渲染法线与几何面方向差异较大；平滑或加权法线也可能产生此提示。"
    ),
    "WINDING_CONFLICT": "两个相邻三角面沿共享边使用了相同绕序，属于拓扑复查项。",
    "NON_MANIFOLD_EDGE": "同一条拓扑边连接了三个或更多三角面。",
    "MISSING_UV_CHANNEL": "所选 UV 通道在该网格中不存在。",
    "INVALID_TRIANGLE_UV": "UV 三角面包含 NaN 或 Infinity 坐标。",
    "DEGENERATE_TRIANGLE_UV": "UV 三角面被压成线、点或接近零面积。",
    "UV_STRETCH": "UV 到实际世界表面的单方向拉伸超过当前阈值。",
}

_ISSUE_DETAIL_MESSAGES_ZH = {
    "NORMAL_ATTRIBUTE_MISSING": "源 MeshDescription LOD0 缺少 Vertex Instance Normal 属性。",
    "NORMAL_NON_FINITE": "源 MeshDescription LOD0 法线包含 NaN 或 Infinity。",
    "NORMAL_ZERO_LENGTH": "源 MeshDescription LOD0 法线为零长度或低于有效阈值。",
    "NORMAL_NON_UNIT": "源 MeshDescription LOD0 法线未归一化；该项作为无效法线警告报告。",
    "VI_NORMAL_DOT_BELOW_MINUS_0_2": (
        "Vertex Instance Normal 与几何面法线点积小于 -0.2，属于明确反向。"
    ),
    "VI_NORMAL_NEAR_PERPENDICULAR": (
        "Vertex Instance Normal 与几何面法线的点积绝对值不大于 0.2，"
        "只作为接近垂直的可疑警告。"
    ),
}


def affected_element_unit(issue_code: str, category: str = "") -> str:
    """Return the display unit for one result group's affected-count field.

    Transform and system rows describe the group as a whole rather than source
    mesh elements, so they intentionally return an empty unit and display as
    an em dash.  Unknown future native codes fall back to the neutral ``项``.
    """

    code = str(issue_code or "").strip().upper()
    normalized_category = str(category or "").strip().lower()
    if code in _NO_AFFECTED_ELEMENT_CODES or normalized_category in {
        "transform",
        "system",
    }:
        return ""
    return _AFFECTED_ELEMENT_UNITS.get(code, "项")


def format_affected_elements(
    issue_code: str, affected_count: int, category: str = ""
) -> str:
    """Format a result group's source-element hit count for artists."""

    unit = affected_element_unit(issue_code, category)
    if not unit:
        return "—"
    return "{} {}".format(max(0, _integer(affected_count)), unit)


def _log(message: str) -> None:
    unreal.log("[MeshQuality] {}".format(message))


def _warn(message: str) -> None:
    unreal.log_warning("[MeshQuality] {}".format(message))


def _object_path(value: Any) -> str:
    if not value:
        return ""
    try:
        return str(value.get_path_name())
    except Exception:
        return str(value)


def _actor_label(actor: Any) -> str:
    for method_name in ("get_actor_label", "get_name"):
        method = getattr(actor, method_name, None)
        if callable(method):
            try:
                return str(method())
            except Exception:
                pass
    return _object_path(actor)


def _actor_subsystem():
    return unreal.get_editor_subsystem(unreal.EditorActorSubsystem)


def _all_level_actors() -> List[Any]:
    return list(_actor_subsystem().get_all_level_actors())


def _selected_level_actors() -> List[Any]:
    return list(_actor_subsystem().get_selected_level_actors())


def _static_mesh_components(actor: Any) -> Iterable[Any]:
    try:
        return list(actor.get_components_by_class(unreal.StaticMeshComponent))
    except Exception:
        return []


def _is_instanced_static_mesh_component(component: Any) -> bool:
    """Return True for ISM/HISM components whose per-instance transform is required.

    One component can render many instances at different transforms.  The
    current result schema stores only a component path, so treating one as a
    regular StaticMeshComponent would make both world-scale analysis and face
    highlighting point at the wrong instance.
    """

    instanced_type = getattr(unreal, "InstancedStaticMeshComponent", None)
    if instanced_type is not None:
        try:
            if isinstance(component, instanced_type):
                return True
        except TypeError:
            pass
    try:
        class_name = str(component.get_class().get_name()).lower()
    except Exception:
        class_name = type(component).__name__.lower()
    return "instancedstaticmeshcomponent" in class_name


def _component_mesh(component: Any):
    try:
        return component.get_editor_property("static_mesh")
    except Exception:
        return getattr(component, "static_mesh", None)


def _vector_tuple(value: Any) -> Tuple[float, float, float]:
    if value is None:
        raise ValueError("vector is None")
    if isinstance(value, (list, tuple)) and len(value) >= 3:
        return float(value[0]), float(value[1]), float(value[2])
    return float(value.x), float(value.y), float(value.z)


def _transform_scale(transform: Any) -> Optional[Tuple[float, float, float]]:
    if transform is None:
        return None
    for property_name in ("scale3d", "scale_3d", "scale"):
        try:
            return _vector_tuple(transform.get_editor_property(property_name))
        except Exception:
            pass
        try:
            return _vector_tuple(getattr(transform, property_name))
        except Exception:
            pass
    return None


def _component_signed_world_scale(component: Any) -> Tuple[float, float, float]:
    """Read component world scale without discarding negative axis signs."""

    for method_name in ("get_world_scale", "get_component_scale"):
        method = getattr(component, method_name, None)
        if callable(method):
            try:
                values = _vector_tuple(method())
                return _rounded_scale(values)
            except Exception:
                pass

    for method_name in (
        "get_component_transform",
        "get_world_transform",
        "get_component_to_world",
    ):
        method = getattr(component, method_name, None)
        if callable(method):
            try:
                values = _transform_scale(method())
                if values is not None:
                    return _rounded_scale(values)
            except Exception:
                pass

    for property_name in ("component_to_world", "component_transform"):
        try:
            values = _transform_scale(component.get_editor_property(property_name))
            if values is not None:
                return _rounded_scale(values)
        except Exception:
            pass

    # Compatibility fallback for older generated Python bindings.  Actor scale
    # is already world-space; multiplying the component-relative scale handles
    # the common StaticMeshActor and Datasmith hierarchy cases.
    try:
        relative = _vector_tuple(component.get_editor_property("relative_scale3d"))
        owner = component.get_owner()
        actor_scale = _vector_tuple(owner.get_actor_scale3d())
        return _rounded_scale(
            tuple(relative[index] * actor_scale[index] for index in range(3))
        )
    except Exception as exc:
        raise RuntimeError(
            "无法读取组件 {} 的有符号世界缩放：{}".format(
                _object_path(component), exc
            )
        )


def _rounded_scale(values: Sequence[float]) -> Tuple[float, float, float]:
    result = []
    for value in values:
        number = round(float(value), 6)
        result.append(0.0 if abs(number) < 1.0e-9 else number)
    return tuple(result)  # type: ignore[return-value]


def _selected_top_roots(selected: Sequence[Any]) -> List[Any]:
    unique = {_object_path(actor): actor for actor in selected if _object_path(actor)}
    selected_paths = set(unique)
    roots = []
    for path, actor in unique.items():
        current = actor
        seen = {path}
        has_selected_parent = False
        while current:
            try:
                current = current.get_attach_parent_actor()
            except Exception:
                current = None
            if not current:
                break
            parent_path = _object_path(current)
            if not parent_path or parent_path in seen:
                break
            seen.add(parent_path)
            if parent_path in selected_paths:
                has_selected_parent = True
                break
        if not has_selected_parent:
            roots.append(actor)
    return roots


def _is_under_roots(actor: Any, root_paths: Sequence[str]) -> bool:
    wanted = set(root_paths)
    seen = set()
    current = actor
    while current:
        path = _object_path(current)
        if path in wanted:
            return True
        if not path or path in seen:
            return False
        seen.add(path)
        try:
            current = current.get_attach_parent_actor()
        except Exception:
            return False
    return False


def _report_progress(
    callback: Optional[Callable[[int, int], bool]], done: int, total: int
) -> None:
    if callback and callback(done, total) is False:
        raise MeshQualityScanCancelled("模型质量扫描已由用户取消。")


def collect_selected_mesh_groups(
    progress_callback: Optional[Callable[[int, int], bool]] = None,
) -> List[MeshAuditGroup]:
    """Collect selected Outliner actors and all attached descendants.

    Components are deduplicated by ``(mesh_path, signed world scale)``.  Every
    actor and component path is retained so one result row can locate all uses.
    """

    selected = _selected_level_actors()
    if not selected:
        raise RuntimeError("请先在 World Outliner 中选择要检查的物体或 Datasmith Root。")
    roots = _selected_top_roots(selected)
    root_paths = [_object_path(actor) for actor in roots]
    actors = _all_level_actors()
    _report_progress(progress_callback, 0, len(actors))

    grouped: Dict[Tuple[str, Tuple[float, float, float]], Dict[str, Any]] = {}
    skipped_instanced_components = 0
    for actor_index, actor in enumerate(actors, 1):
        if _is_under_roots(actor, root_paths):
            actor_path = _object_path(actor)
            actor_label = _actor_label(actor)
            for component in _static_mesh_components(actor):
                if _is_instanced_static_mesh_component(component):
                    skipped_instanced_components += 1
                    continue
                mesh = _component_mesh(component)
                if not mesh:
                    continue
                mesh_path = _object_path(mesh)
                if not mesh_path:
                    continue
                transform_determinant, orientation_reversing = (
                    _analyze_scene_component_transform(component)
                )
                world_scale = _component_signed_world_scale(component)
                key = (mesh_path, world_scale)
                group = grouped.setdefault(
                    key,
                    {
                        "mesh": mesh,
                        "mesh_path": mesh_path,
                        "mesh_name": str(mesh.get_name()),
                        "world_scale": world_scale,
                        "uses": {},
                    },
                )
                component_path = _object_path(component)
                use_key = component_path or "{}:{}".format(
                    actor_path, str(component.get_name())
                )
                group["uses"].setdefault(
                    use_key,
                    MeshComponentUse(
                        actor_path=actor_path,
                        actor_label=actor_label,
                        component_path=component_path,
                        component_name=str(component.get_name()),
                        transform_determinant=transform_determinant,
                        orientation_reversing=orientation_reversing,
                    ),
                )
        if actor_index % SCAN_PROGRESS_INTERVAL == 0 or actor_index == len(actors):
            _report_progress(progress_callback, actor_index, len(actors))

    groups = [
        MeshAuditGroup(
            mesh_path=item["mesh_path"],
            mesh_name=item["mesh_name"],
            world_scale=item["world_scale"],
            uses=sorted(
                item["uses"].values(),
                key=lambda use: (use.actor_label.lower(), use.component_name.lower()),
            ),
            mesh=item["mesh"],
        )
        for item in grouped.values()
    ]
    groups.sort(
        key=lambda item: (item.mesh_name.lower(), item.mesh_path, item.world_scale)
    )
    if not groups:
        if skipped_instanced_components:
            raise RuntimeError(
                "当前选择只包含 ISM/HISM 实例化网格。为避免错误分析或把红色高亮画到错误实例，"
                "暂不扫描这类组件；请转换/拆分为普通 Static Mesh Actor 后再检查。"
            )
        raise RuntimeError("当前选择及其子级中没有找到可检查的 Static Mesh Component。")
    if skipped_instanced_components:
        _warn(
            "已跳过 {} 个 ISM/HISM 实例化组件：当前报告没有单实例索引与变换，"
            "继续分析会导致数值或红色实体面定位错误。".format(
                skipped_instanced_components
            )
        )
    return groups


def options_for_preset(name: str = "standard") -> MeshQualityOptions:
    """Return one of the artist-facing lenient/standard/strict presets."""

    token = str(name or "standard").strip().lower()
    if token in ("lenient", "loose", "宽松"):
        return MeshQualityOptions(
            normal_unit_tolerance=0.1,
            report_non_unit_normals=False,
            normal_dot_warning_threshold=0.2,
            normal_dot_error_threshold=-0.2,
            report_ambiguous_normals=True,
            report_normal_direction_mismatch=False,
            uv_stretch_warning_threshold=4.0,
            uv_stretch_error_threshold=8.0,
            non_uniform_scale_tolerance=0.01,
        )
    if token in ("strict", "严格"):
        return MeshQualityOptions(
            normal_unit_tolerance=0.025,
            report_non_unit_normals=True,
            normal_dot_warning_threshold=0.2,
            normal_dot_error_threshold=-0.2,
            report_ambiguous_normals=True,
            report_normal_direction_mismatch=True,
            uv_stretch_warning_threshold=2.0,
            uv_stretch_error_threshold=4.0,
            non_uniform_scale_tolerance=0.0001,
        )
    if token not in ("standard", "default", "标准"):
        raise ValueError("未知模型质量预设：{}".format(name))
    return MeshQualityOptions()


def _coerce_options(value: Optional[Any]) -> MeshQualityOptions:
    if value is None:
        return MeshQualityOptions()
    if isinstance(value, MeshQualityOptions):
        return value
    if isinstance(value, Mapping):
        valid = {item.name for item in fields(MeshQualityOptions)}
        unknown = sorted(set(value) - valid)
        if unknown:
            raise ValueError("未知模型质量选项：{}".format("、".join(unknown)))
        return MeshQualityOptions(**dict(value))
    raise TypeError("options 必须是 MeshQualityOptions、字典或 None。")


def _native_api():
    options_class = getattr(unreal, "DatasmithMeshAuditOptions", None)
    library = getattr(unreal, "DatasmithMeshAuditLibrary", None)
    analyze = getattr(library, "analyze_static_mesh", None) if library else None
    analyze_transform = (
        getattr(library, "analyze_scene_component_transform", None)
        if library
        else None
    )
    if (
        options_class is None
        or not callable(analyze)
        or not callable(analyze_transform)
    ):
        missing = []
        if options_class is None:
            missing.append("DatasmithMeshAuditOptions")
        if not callable(analyze):
            missing.append("analyze_static_mesh")
        if not callable(analyze_transform):
            missing.append("analyze_scene_component_transform")
        raise NativeAuditUnavailableError(
            "原生审计 DLL 与 {} Python 不兼容：缺少 {}。"
            "请完整更新插件的 Binaries、Content/Python 和 Source 后重启 UE；"
            "为避免把组件矩阵镜像误判为源资产法线问题，本版本不会退回到"
            "世界缩放 XYZ 乘积判断。".format(
                version.PLUGIN_VERSION,
                "、".join(missing),
            )
        )
    return options_class, analyze


def _analyze_scene_component_transform(
    component: Any,
) -> Tuple[float, bool]:
    """Return the native full-matrix determinant and orientation sign.

    A component can inherit rotation, non-uniform scale, mirroring or shear
    from several parents.  Multiplying the three scale values is therefore
    not an acceptable substitute for the determinant of ComponentToWorld.
    The generated USTRUCT field names are read tolerantly, but the two pieces
    of semantic data are mandatory and cross-checked.
    """

    # _native_api performs the explicit stale-DLL compatibility check.
    _native_api()
    library = getattr(unreal, "DatasmithMeshAuditLibrary", None)
    analyze = getattr(library, "analyze_scene_component_transform", None)
    try:
        result = analyze(component)
    except Exception as exc:
        raise RuntimeError(
            "读取组件完整变换矩阵失败（{}）：{}".format(
                _object_path(component) or "未知组件",
                exc,
            )
        ) from exc

    missing = object()
    success = _read_property(result, ("success",), missing)
    error = str(_read_property(result, ("error", "message"), "") or "").strip()
    if success is not missing and not _boolean(success, default=False):
        raise RuntimeError(
            "原生组件变换检查失败（{}）：{}".format(
                _object_path(component) or "未知组件",
                error or "未提供错误信息",
            )
        )

    determinant_value = _read_property(
        result,
        (
            "determinant",
            "transform_determinant",
            "matrix_determinant",
        ),
        missing,
    )
    negative_value = _read_property(
        result,
        (
            "negative_determinant",
            "is_negative_determinant",
            "orientation_reversing",
            "is_orientation_reversing",
        ),
        missing,
    )
    if determinant_value is missing or negative_value is missing:
        absent = []
        if determinant_value is missing:
            absent.append("determinant")
        if negative_value is missing:
            absent.append("negative_determinant/orientation_reversing")
        raise NativeAuditUnavailableError(
            "原生审计 DLL 与 {} Python 不兼容："
            "analyze_scene_component_transform 结果缺少 {}。"
            "请完整更新插件并重启 UE。".format(
                version.PLUGIN_VERSION,
                "、".join(absent),
            )
        )

    try:
        determinant = float(determinant_value)
    except Exception as exc:
        raise RuntimeError(
            "组件变换矩阵行列式不是有效数值：{!r}".format(determinant_value)
        ) from exc
    if not math.isfinite(determinant):
        raise RuntimeError(
            "组件变换矩阵行列式包含 NaN/Infinity：{!r}".format(
                determinant_value
            )
        )

    orientation_reversing = determinant < 0.0
    native_negative = _boolean(negative_value)
    if native_negative != orientation_reversing:
        raise RuntimeError(
            "原生组件变换结果自相矛盾：determinant={:.9g}，"
            "negative_determinant={}。请更新完整插件后重启 UE。".format(
                determinant,
                native_negative,
            )
        )
    return determinant, orientation_reversing


def native_audit_available() -> bool:
    """Return whether the compiled native audit API is available."""

    try:
        _native_api()
        return True
    except NativeAuditUnavailableError:
        return False


def _set_native_property(target: Any, name: str, value: Any, required: bool) -> bool:
    setter = getattr(target, "set_editor_property", None)
    if callable(setter):
        try:
            setter(name, value)
            return True
        except Exception:
            pass
    try:
        setattr(target, name, value)
        return True
    except Exception:
        if required:
            raise RuntimeError("原生审计选项不支持属性 {}。".format(name))
        return False


def _native_values_match(expected: Any, actual: Any) -> bool:
    """Compare generated USTRUCT values without depending on wrapper reprs."""

    if isinstance(expected, bool):
        return isinstance(actual, (bool, int)) and bool(actual) is expected
    if isinstance(expected, int) and not isinstance(expected, bool):
        try:
            return int(actual) == expected
        except Exception:
            return False
    if isinstance(expected, float):
        try:
            return math.isclose(float(actual), expected, rel_tol=1.0e-9, abs_tol=1.0e-12)
        except Exception:
            return False
    if all(hasattr(expected, name) for name in ("x", "y", "z")):
        try:
            return all(
                math.isclose(
                    float(getattr(actual, name)),
                    float(getattr(expected, name)),
                    rel_tol=1.0e-9,
                    abs_tol=1.0e-12,
                )
                for name in ("x", "y", "z")
            )
        except Exception:
            return False
    return actual == expected


def _verify_native_property(target: Any, name: str, expected: Any) -> None:
    missing = object()
    actual = _read_property(target, (name,), missing)
    if actual is missing or not _native_values_match(expected, actual):
        raise RuntimeError(
            "原生审计 DLL 与 Python 不兼容：关键选项 {} 写入后无法读回"
            "（期望 {!r}，实际 {!r}）。请完整更新插件 Binaries/Source 并重启 UE。".format(
                name,
                expected,
                "<缺失>" if actual is missing else actual,
            )
        )


def build_native_options(
    options: Optional[Any], world_scale: Sequence[float]
) -> Any:
    """Create and populate one native options struct for an audit group."""

    options_class, _analyze = _native_api()
    settings = _coerce_options(options)
    native = options_class()
    for name in _NATIVE_OPTION_NAMES:
        value = getattr(settings, name)
        required = name in _REQUIRED_NATIVE_OPTION_NAMES
        written = _set_native_property(native, name, value, required=required)
        if required:
            if not written:
                raise RuntimeError(
                    "原生审计 DLL 与 Python 不兼容：缺少关键选项 {}。"
                    "请完整更新插件并重启 UE。".format(name)
                )
            _verify_native_property(native, name, value)
    native_world_scale = unreal.Vector(
        *tuple(float(value) for value in world_scale)
    )
    _set_native_property(
        native,
        "world_scale",
        native_world_scale,
        required=True,
    )
    _verify_native_property(native, "world_scale", native_world_scale)
    for name, value in settings.native_overrides.items():
        _set_native_property(native, str(name), value, required=True)
        _verify_native_property(native, str(name), value)
    return native


def _read_property(value: Any, names: Sequence[str], default: Any = None) -> Any:
    if value is None:
        return default
    for name in names:
        if isinstance(value, Mapping) and name in value:
            return value[name]
        getter = getattr(value, "get_editor_property", None)
        if callable(getter):
            try:
                return getter(name)
            except Exception:
                pass
        try:
            return getattr(value, name)
        except Exception:
            pass
    return default


def _enum_token(value: Any) -> str:
    text = str(value or "").strip().upper()
    if not text:
        return ""
    text = text.replace("<", "").replace(">", "")
    text = text.split(":", 1)[0].strip()
    return text.split(".")[-1]


def _severity(value: Any) -> str:
    token = _enum_token(value)
    if "CRITICAL" in token or "ERROR" in token or "FATAL" in token:
        return "error"
    if "WARN" in token:
        return "warning"
    return "info"


def _finite_float(value: Any, default: float = 0.0) -> float:
    try:
        result = float(value)
        return result if math.isfinite(result) else default
    except Exception:
        return default


def _integer(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except Exception:
        return default


def _integer_list(value: Any) -> List[int]:
    if value is None:
        return []
    try:
        return [_integer(item) for item in list(value)]
    except Exception:
        return []


def _boolean(value: Any, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    token = str(value or "").strip().lower()
    if token in {"true", "1", "yes", "on"}:
        return True
    if token in {"false", "0", "no", "off", ""}:
        return False
    return bool(default)


def _vector_list(value: Any) -> List[Tuple[float, float, float]]:
    if value is None:
        return []
    result = []
    try:
        source = list(value)
    except Exception:
        return result
    for item in source:
        try:
            result.append(tuple(_finite_float(number) for number in _vector_tuple(item)))
        except Exception:
            continue
    return result


def _safe_vector_tuple(
    value: Any, default: Tuple[float, float, float] = (0.0, 0.0, 0.0)
) -> Tuple[float, float, float]:
    try:
        result = tuple(_finite_float(number) for number in _vector_tuple(value))
        return result  # type: ignore[return-value]
    except Exception:
        return tuple(default)


def _sample_bounds(
    vertices: Sequence[Tuple[float, float, float]],
) -> Tuple[Tuple[float, float, float], Tuple[float, float, float]]:
    if not vertices:
        zero = (0.0, 0.0, 0.0)
        return zero, zero
    minimum = tuple(min(point[axis] for point in vertices) for axis in range(3))
    maximum = tuple(max(point[axis] for point in vertices) for axis in range(3))
    return minimum, maximum


def _result_issues(result: Any) -> List[Any]:
    issues = _read_property(result, ("issues", "audit_issues"), None)
    if issues is not None:
        try:
            return list(issues)
        except Exception:
            return []
    if isinstance(result, (list, tuple)):
        # Generated bindings occasionally return (success, result) when a C++
        # function contains out parameters.  Prefer a nested result with issues.
        for item in result:
            nested = _read_property(item, ("issues", "audit_issues"), None)
            if nested is not None:
                return list(nested)
        if not result or not isinstance(result[0], bool):
            return list(result)
    return []


def _result_summary(result: Any) -> Dict[str, Any]:
    summary = {}
    for name in _RESULT_SUMMARY_FIELDS:
        summary[name] = _read_property(result, (name,), None)
    if isinstance(result, (list, tuple)):
        for item in result:
            if _read_property(item, ("issues",), None) is not None:
                return _result_summary(item)
    return summary


def _group_values(group: MeshAuditGroup) -> Dict[str, Any]:
    uses = group.uses
    first = uses[0] if uses else MeshComponentUse("", "", "", "")
    return {
        "mesh_path": group.mesh_path,
        "mesh_name": group.mesh_name,
        "world_scale": tuple(group.world_scale),
        "actor_path": first.actor_path,
        "actor_label": first.actor_label,
        "component_path": first.component_path,
        "component_name": first.component_name,
        "actor_paths": sorted({item.actor_path for item in uses if item.actor_path}),
        "actor_labels": sorted({item.actor_label for item in uses if item.actor_label}),
        "component_paths": sorted(
            {item.component_path for item in uses if item.component_path}
        ),
        "component_names": sorted(
            {item.component_name for item in uses if item.component_name}
        ),
        "component_count": len(uses),
    }


def _component_use_values(
    group: MeshAuditGroup, use: MeshComponentUse
) -> Dict[str, Any]:
    """Return row identity scoped to exactly one Actor component use."""

    return {
        "mesh_path": group.mesh_path,
        "mesh_name": group.mesh_name,
        "world_scale": tuple(group.world_scale),
        "actor_path": use.actor_path,
        "actor_label": use.actor_label,
        "component_path": use.component_path,
        "component_name": use.component_name,
        "actor_paths": [use.actor_path] if use.actor_path else [],
        "actor_labels": [use.actor_label] if use.actor_label else [],
        "component_paths": [use.component_path] if use.component_path else [],
        "component_names": [use.component_name] if use.component_name else [],
        "component_count": 1,
    }


def _issue_category(code: str, issue_type: str) -> str:
    token = "{}_{}".format(code, issue_type).upper()
    explicit = {
        "INVALID_NORMAL": "normal",
        "VERTEX_NORMAL_REVERSED": "normal",
        "CLOSED_SHELL_INWARD": "normal",
        "OPEN_MESH_DIRECTION_UNKNOWN": "normal",
        "DEGENERATE_TRIANGLE": "geometry",
        "LOCAL_WINDING_CONFLICT": "geometry",
        "NON_MANIFOLD_TOPOLOGY": "geometry",
        "ACTOR_NEGATIVE_SCALE": "transform",
    }
    for issue_code, category in explicit.items():
        if issue_code in token:
            return category
    # A non-manifold edge is a topology/geometry defect.  It does not describe
    # the direction or validity of a stored normal, even though it is often
    # discovered while diagnosing shading artifacts.
    if "NON_MANIFOLD" in token:
        return "geometry"
    if "UV" in token:
        return "uv"
    if "NORMAL" in token:
        return "normal"
    if "WINDING" in token:
        return "geometry"
    return "geometry"


def _native_issue_row(
    group: MeshAuditGroup, issue: Any, summary: Mapping[str, Any]
) -> MeshQualityRow:
    issue_type = _enum_token(_read_property(issue, ("issue_type", "type"), ""))
    issue_code = _enum_token(
        _read_property(issue, ("issue_code", "code"), issue_type) or issue_type
    )
    native_message = str(
        _read_property(issue, ("description", "message", "reason"), issue_code)
        or issue_code
    )
    detail_code = _enum_token(_read_property(issue, ("detail_code",), ""))
    if detail_code in {"NONE", "NAME_NONE"}:
        detail_code = ""
    message = _ISSUE_DETAIL_MESSAGES_ZH.get(
        detail_code,
        _ISSUE_MESSAGES_ZH.get(issue_code, native_message),
    )
    common = _group_values(group)
    retained_triangle_ids = _integer_list(
        _read_property(issue, ("triangle_ids", "triangle_indices"), [])
    )
    missing = object()
    raw_error_triangle_ids = _read_property(
        issue, ("error_triangle_ids",), missing
    )
    error_triangle_ids = (
        list(retained_triangle_ids)
        if raw_error_triangle_ids is missing
        else _integer_list(raw_error_triangle_ids)
    )
    sample_vertices = _vector_list(
        _read_property(issue, ("sample_vertices",), [])
    )
    sample_face_normals = _vector_list(
        _read_property(issue, ("sample_face_normals",), [])
    )
    derived_bounds_min, derived_bounds_max = _sample_bounds(sample_vertices)
    raw_samples_truncated = _read_property(issue, ("samples_truncated",), missing)
    samples_truncated = (
        len(error_triangle_ids) > len(sample_vertices) // 3
        if raw_samples_truncated is missing
        else _boolean(raw_samples_truncated)
    )
    severity = _severity(_read_property(issue, ("severity",), "warning"))
    if issue_code == "OPEN_MESH_DIRECTION_UNKNOWN":
        # Open surfaces have no provable outside direction.  Never turn this
        # informational topology state into an error based on a stale DLL.
        severity = "info"
    return MeshQualityRow(
        severity=severity,
        category=_issue_category(issue_code, issue_type),
        issue_code=issue_code,
        issue_type=issue_type,
        message=message,
        mesh_triangle_count=_integer(summary.get("triangle_count")),
        uv_channel_count=_integer(summary.get("uv_channel_count")),
        material_slot=_integer(
            _read_property(issue, ("material_slot", "material_index"), -1), -1
        ),
        detail_code=detail_code,
        affected_count=_integer(_read_property(issue, ("count",), 0)),
        max_metric=_finite_float(_read_property(issue, ("max_metric", "metric"), 0.0)),
        lod_index=_integer(
            _read_property(issue, ("lod_index",), summary.get("lod_index")), 0
        ),
        region_id=str(_read_property(issue, ("region_id",), "") or ""),
        region_index=_integer(_read_property(issue, ("region_index",), -1), -1),
        error_triangle_ids=error_triangle_ids,
        triangle_id_space=str(
            _read_property(issue, ("triangle_id_space",), "") or ""
        ),
        local_bounds_min=_safe_vector_tuple(
            _read_property(issue, ("local_bounds_min",), derived_bounds_min),
            derived_bounds_min,
        ),
        local_bounds_max=_safe_vector_tuple(
            _read_property(issue, ("local_bounds_max",), derived_bounds_max),
            derived_bounds_max,
        ),
        samples_truncated=samples_truncated,
        triangle_ids=retained_triangle_ids,
        sample_vertices=sample_vertices,
        sample_face_normals=sample_face_normals,
        analysis_milliseconds=_finite_float(summary.get("analysis_milliseconds")),
        source="native",
        details={
            "native_mesh_path": str(summary.get("mesh_path") or group.mesh_path),
            "normal_data_source": str(
                summary.get("normal_data_source") or "unknown"
            ),
            "native_message": native_message,
            "native_region_fields": raw_error_triangle_ids is not missing,
        },
        **common,
    )


def _synthetic_row(
    group: MeshAuditGroup,
    severity: str,
    code: str,
    message: str,
    max_metric: float = 0.0,
    details: Optional[Dict[str, Any]] = None,
    component_use: Optional[MeshComponentUse] = None,
) -> MeshQualityRow:
    return MeshQualityRow(
        severity=severity,
        category="transform",
        issue_code=code,
        issue_type=code,
        message=message,
        max_metric=_finite_float(max_metric),
        source="instance_transform",
        details=details or {},
        **(
            _component_use_values(group, component_use)
            if component_use is not None
            else _group_values(group)
        ),
    )


def _scale_issue_rows(
    group: MeshAuditGroup, settings: MeshQualityOptions
) -> List[MeshQualityRow]:
    del settings  # Reserved for compatibility with the public helper signature.
    rows = []
    # Actor mirroring is instance-specific.  Never infer it from the three
    # displayed scale values: inherited transforms and matrix composition can
    # change the determinant sign.  Collection already obtained the native
    # ComponentToWorld determinant for every ordinary StaticMeshComponent.
    for use in group.uses:
        if not use.orientation_reversing:
            continue
        rows.append(
            _synthetic_row(
                group,
                "warning",
                "ACTOR_NEGATIVE_SCALE",
                "Actor/组件完整变换矩阵的行列式为 {:.9g}（小于 0），"
                "该场景实例发生镜像并导致朝向翻转；此结果只属于当前组件，"
                "不会合并为源 Static Mesh 法线错误。".format(
                    use.transform_determinant
                ),
                max_metric=abs(use.transform_determinant),
                details={
                    "transform_determinant": use.transform_determinant,
                    "negative_determinant": True,
                    "orientation_reversing": True,
                    "detection": "native_component_to_world_matrix_determinant",
                },
                component_use=use,
            )
        )
    return rows


def _analysis_failure_row(
    group: MeshAuditGroup, message: str, summary: Optional[Mapping[str, Any]] = None
) -> MeshQualityRow:
    summary = summary or {}
    return MeshQualityRow(
        severity="error",
        category="system",
        issue_code="NATIVE_ANALYSIS_FAILED",
        issue_type="NATIVE_ANALYSIS_FAILED",
        message=message,
        mesh_triangle_count=_integer(summary.get("triangle_count")),
        uv_channel_count=_integer(summary.get("uv_channel_count")),
        analysis_milliseconds=_finite_float(summary.get("analysis_milliseconds")),
        source="native",
        details={"native_error": str(summary.get("error") or message)},
        **_group_values(group),
    )


def analyze_mesh_groups(
    groups: Sequence[MeshAuditGroup],
    options: Optional[Any] = None,
    progress_callback: Optional[Callable[[int, int], bool]] = None,
) -> List[MeshQualityRow]:
    """Analyze already collected groups without modifying any editor asset."""

    _options_class, analyze = _native_api()
    settings = _coerce_options(options)
    groups = list(groups)
    rows: List[MeshQualityRow] = []
    _report_progress(progress_callback, 0, len(groups))
    for group_index, group in enumerate(groups, 1):
        summary: Dict[str, Any] = {}
        group.audit_result_summary = {}
        actor_labels = [use.actor_label for use in group.uses if use.actor_label]
        shown_actors = actor_labels[:12]
        actor_text = "、".join(shown_actors) or "<无 Actor 标签>"
        if len(actor_labels) > len(shown_actors):
            actor_text += " 等 {} 个".format(len(actor_labels))
        option_log = {
            name: getattr(settings, name)
            for name in _NATIVE_OPTION_NAMES
            if hasattr(settings, name)
        }
        option_log["include_scale_issues"] = settings.include_scale_issues
        _log(
            "扫描组 {}/{}｜Actor={}｜Mesh={}｜Scale={}｜Options={}".format(
                group_index,
                len(groups),
                actor_text,
                group.mesh_path,
                tuple(group.world_scale),
                json.dumps(option_log, ensure_ascii=False, sort_keys=True),
            )
        )
        group_rows: List[MeshQualityRow] = []
        try:
            native_options = build_native_options(settings, group.world_scale)
            result = analyze(group.mesh, native_options)
            summary = _result_summary(result)
            missing_direction_fields = [
                name
                for name in _REQUIRED_DIRECTION_SUMMARY_FIELDS
                if summary.get(name) is None
            ]
            if missing_direction_fields:
                raise NativeAuditUnavailableError(
                    "原生审计 DLL 与 {} Python 不兼容：结果缺少 {}。"
                    "这些字段用于明确显示未覆盖的封闭壳体，不能静默当作"
                    "方向检查全部通过。请完整更新插件并重启 UE。".format(
                        version.PLUGIN_VERSION,
                        "、".join(missing_direction_fields),
                    )
                )
            group.audit_result_summary = dict(summary)
            success = summary.get("success")
            error = str(summary.get("error") or "").strip()
            if success is False:
                group_rows.append(
                    _analysis_failure_row(
                        group,
                        "原生审计失败：{}".format(error or "未提供错误信息"),
                        summary,
                    )
                )
            else:
                group_rows.extend(
                    _native_issue_row(group, issue, summary)
                    for issue in _result_issues(result)
                )
                # Source-mesh analysis is shared by every use and therefore
                # cannot own an Actor/component transform result.  Transitional
                # native builds may still emit the legacy aggregate code from
                # Options.WorldScale; discard it and use the per-component full
                # matrix audit collected above.
                if any(
                    row.issue_code == "ACTOR_NEGATIVE_SCALE"
                    for row in group_rows
                ):
                    _warn(
                        "原生 AnalyzeStaticMesh 返回了旧版聚合的 "
                        "ACTOR_NEGATIVE_SCALE；已忽略，并改用每个组件的完整"
                        " ComponentToWorld 矩阵行列式。"
                    )
                    group_rows = [
                        row
                        for row in group_rows
                        if row.issue_code != "ACTOR_NEGATIVE_SCALE"
                    ]
        except NativeAuditUnavailableError:
            raise
        except Exception as exc:
            group_rows.append(
                _analysis_failure_row(
                    group,
                    "原生审计调用异常：{}".format(exc),
                    summary,
                )
            )

        if settings.include_scale_issues:
            native_codes = {row.issue_code for row in group_rows}
            scale_rows = _scale_issue_rows(group, settings)
            group_rows.extend(
                row
                for row in scale_rows
                if row.issue_code == "ACTOR_NEGATIVE_SCALE"
                or row.issue_code not in native_codes
            )
        rows.extend(group_rows)
        if group_rows:
            _log(
                "扫描组结果｜Actor={}｜Mesh={}｜Scale={}｜结果组={}｜Codes={}".format(
                    actor_text,
                    group.mesh_path,
                    tuple(group.world_scale),
                    len(group_rows),
                    ",".join(row.issue_code for row in group_rows),
                )
            )
        else:
            _log(
                "扫描组结果 0｜Actor={}｜Mesh={}｜Scale={}｜Options={}".format(
                    actor_text,
                    group.mesh_path,
                    tuple(group.world_scale),
                    json.dumps(option_log, ensure_ascii=False, sort_keys=True),
                )
            )
        _report_progress(progress_callback, group_index, len(groups))

    priority = {"error": 0, "warning": 1, "info": 2}
    rows.sort(
        key=lambda row: (
            priority.get(row.severity, 9),
            row.mesh_name.lower(),
            row.issue_code,
            row.lod_index,
            row.region_index,
            row.region_id,
            row.world_scale,
        )
    )
    return rows


def summarize_audit_groups(
    groups: Sequence[MeshAuditGroup],
) -> Dict[str, Any]:
    """Aggregate native closed-shell coverage independently of issue rows.

    A valid audit can produce zero issue rows while still abstaining from an
    absolute closed-shell direction decision.  Keeping this information on the
    analyzed group prevents the UI/report from presenting abstention as a full
    pass or inventing a ninth issue type.
    """

    candidate_count = 0
    evaluated_count = 0
    abstained_count = 0
    reason_counter: Counter = Counter()
    analyzed_group_count = 0
    for group in groups:
        summary = dict(getattr(group, "audit_result_summary", None) or {})
        if not summary:
            continue
        analyzed_group_count += 1
        candidate_count += _integer(
            summary.get("closed_shell_direction_candidate_count")
        )
        evaluated_count += _integer(
            summary.get("closed_shell_direction_evaluated_count")
        )
        abstained_count += _integer(
            summary.get("closed_shell_direction_abstained_count")
        )
        raw_reasons = summary.get("direction_abstention_reasons") or []
        try:
            reasons = list(raw_reasons)
        except Exception:
            reasons = [raw_reasons]
        for reason in reasons:
            token = str(reason or "").strip()
            if token:
                reason_counter[token] += 1
    return {
        "analyzed_group_count": analyzed_group_count,
        "closed_shell_direction_candidate_count": candidate_count,
        "closed_shell_direction_evaluated_count": evaluated_count,
        "closed_shell_direction_abstained_count": abstained_count,
        "direction_abstention_reasons": sorted(reason_counter),
        "direction_abstention_reason_counts": dict(
            sorted(reason_counter.items(), key=lambda item: item[0])
        ),
    }


def scan_selected_mesh_quality(
    options: Optional[Any] = None,
    collect_progress_callback: Optional[Callable[[int, int], bool]] = None,
    analyze_progress_callback: Optional[Callable[[int, int], bool]] = None,
) -> List[MeshQualityRow]:
    """Collect the current selection and return UI-ready issue rows."""

    # Check before walking the level so a missing binary produces the clearest
    # possible response instead of looking like an empty/slow scan.
    _native_api()
    groups = collect_selected_mesh_groups(collect_progress_callback)
    _log(
        "扫描到 {} 个唯一 Static Mesh + world scale 组合。".format(len(groups))
    )
    return analyze_mesh_groups(groups, options, analyze_progress_callback)


def select_mesh_quality_row_actors(
    rows: Sequence[MeshQualityRow], focus_viewport: bool = False
) -> int:
    """Select every still-valid Actor referenced by the supplied rows."""

    wanted = {
        path for row in rows for path in row.actor_paths if str(path or "").strip()
    }
    actors = [actor for actor in _all_level_actors() if _object_path(actor) in wanted]
    _actor_subsystem().set_selected_level_actors(actors)
    if focus_viewport and actors:
        _focus_viewport_on_actors(actors)
    return len(actors)


def _focus_viewport_on_actors(actors: Sequence[Any]) -> bool:
    subsystem_class = getattr(unreal, "LevelEditorSubsystem", None)
    if subsystem_class is None:
        return False
    try:
        subsystem = unreal.get_editor_subsystem(subsystem_class)
    except Exception:
        return False
    for method_name in (
        "editor_focus_viewport_on_actors",
        "focus_viewport_on_actors",
    ):
        method = getattr(subsystem, method_name, None)
        if not callable(method):
            continue
        for arguments in ((list(actors),), (list(actors), False)):
            try:
                method(*arguments)
                return True
            except Exception:
                pass
    return False


def sync_mesh_quality_assets(rows: Sequence[MeshQualityRow]) -> int:
    """Locate unique Static Mesh assets in the Content Browser."""

    paths = sorted({row.mesh_path for row in rows if row.mesh_path})
    object_paths = []
    for path in paths:
        try:
            asset = unreal.EditorAssetLibrary.load_asset(path)
        except Exception:
            asset = None
        if asset:
            object_paths.append(_object_path(asset))
    if not object_paths:
        return 0
    sync = getattr(unreal.EditorAssetLibrary, "sync_browser_to_objects", None)
    if not callable(sync):
        raise RuntimeError("当前 UE 版本没有 Content Browser 定位接口。")
    sync(object_paths)
    return len(object_paths)


def locate_mesh_quality_rows(
    rows: Sequence[MeshQualityRow],
    focus_viewport: bool = True,
    sync_content_browser: bool = True,
) -> Dict[str, Any]:
    """Select level uses and optionally locate their mesh assets."""

    rows = list(rows)
    actor_count = select_mesh_quality_row_actors(rows, focus_viewport)
    asset_count = sync_mesh_quality_assets(rows) if sync_content_browser else 0
    return {
        "actor_count": actor_count,
        "asset_count": asset_count,
        "focused": bool(actor_count and focus_viewport),
    }


def locate_mesh_quality_row(
    row: MeshQualityRow,
    focus_viewport: bool = True,
    sync_content_browser: bool = True,
) -> Dict[str, Any]:
    """Convenience singular form used by a table row's Locate button."""

    return locate_mesh_quality_rows([row], focus_viewport, sync_content_browser)


def mesh_quality_row_to_dict(row: MeshQualityRow) -> Dict[str, Any]:
    """Convert a public row to a JSON-safe dictionary."""

    data = asdict(row)
    data["world_scale"] = list(row.world_scale)
    data["sample_vertices"] = [list(value) for value in row.sample_vertices]
    data["sample_face_normals"] = [
        list(value) for value in row.sample_face_normals
    ]
    return data


def _engine_version() -> str:
    try:
        return str(unreal.SystemLibrary.get_engine_version())
    except Exception:
        return "Unknown"


def _project_key() -> str:
    try:
        path = str(unreal.Paths.get_project_file_path())
        return os.path.splitext(os.path.basename(path))[0] or "UnrealProject"
    except Exception:
        return "UnrealProject"


def _ensure_writable_directory(path: str) -> str:
    os.makedirs(path, exist_ok=True)
    handle, probe = tempfile.mkstemp(prefix=".dma_write_test_", dir=path)
    os.close(handle)
    os.unlink(probe)
    return path


def resolve_mesh_quality_report_directory(output_dir: Optional[str] = None) -> str:
    """Resolve a writable report directory with a per-user fallback."""

    if output_dir:
        try:
            return _ensure_writable_directory(os.path.abspath(output_dir))
        except Exception as exc:
            raise RuntimeError("报告目录不可写：{}（{}）".format(output_dir, exc))

    candidates = []
    try:
        saved = unreal.Paths.convert_relative_path_to_full(
            unreal.Paths.project_saved_dir()
        )
        candidates.append(os.path.join(saved, TOOL_NAME, "MeshQualityReports"))
    except Exception:
        pass
    local = os.environ.get("LOCALAPPDATA") or tempfile.gettempdir()
    candidates.append(
        os.path.join(local, TOOL_NAME, _project_key(), "MeshQualityReports")
    )
    errors = []
    for candidate in candidates:
        try:
            return _ensure_writable_directory(candidate)
        except Exception as exc:
            errors.append("{}: {}".format(candidate, exc))
    raise RuntimeError("无法创建可写的模型质量报告目录：{}".format("；".join(errors)))


def _atomic_write(path: str, content: bytes) -> None:
    directory = os.path.dirname(path)
    handle, temporary = tempfile.mkstemp(prefix=".dma_report_", dir=directory)
    try:
        with os.fdopen(handle, "wb") as output:
            output.write(content)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
    except Exception:
        try:
            os.unlink(temporary)
        except Exception:
            pass
        raise


def _report_summary(
    rows: Sequence[MeshQualityRow],
    scan_summary: Optional[Mapping[str, Any]] = None,
) -> Dict[str, Any]:
    region_keys = {
        (
            row.mesh_path,
            row.lod_index,
            row.region_id,
            row.region_index,
            row.issue_code,
            row.detail_code,
        )
        for row in rows
        if row.region_id or row.region_index >= 0
    }
    summary = {
        "row_count": len(rows),
        "unique_mesh_count": len({row.mesh_path for row in rows}),
        "unique_component_count": len(
            {path for row in rows for path in row.component_paths}
        ),
        "region_result_count": len(region_keys),
        "complete_error_triangle_count": sum(
            len(set(row.error_triangle_ids)) for row in rows
        ),
        "truncated_sample_result_count": sum(
            bool(row.samples_truncated) for row in rows
        ),
        "severity_counts": dict(Counter(row.severity for row in rows)),
        "category_counts": dict(Counter(row.category for row in rows)),
    }
    coverage = dict(scan_summary or {})
    summary.update(
        {
            "analyzed_group_count": _integer(
                coverage.get("analyzed_group_count")
            ),
            "closed_shell_direction_candidate_count": _integer(
                coverage.get("closed_shell_direction_candidate_count")
            ),
            "closed_shell_direction_evaluated_count": _integer(
                coverage.get("closed_shell_direction_evaluated_count")
            ),
            "closed_shell_direction_abstained_count": _integer(
                coverage.get("closed_shell_direction_abstained_count")
            ),
            "direction_abstention_reasons": list(
                coverage.get("direction_abstention_reasons") or []
            ),
            "direction_abstention_reason_counts": dict(
                coverage.get("direction_abstention_reason_counts") or {}
            ),
        }
    )
    return summary


def export_mesh_quality_report(
    rows: Sequence[MeshQualityRow],
    output_dir: Optional[str] = None,
    scan_summary: Optional[Mapping[str, Any]] = None,
) -> Tuple[str, str]:
    """Write schema-versioned JSON and Excel-friendly UTF-8 CSV reports."""

    rows = list(rows)
    directory = resolve_mesh_quality_report_directory(output_dir)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    json_path = os.path.join(directory, "mesh_quality_{}.json".format(stamp))
    csv_path = os.path.join(directory, "mesh_quality_{}.csv".format(stamp))
    payload = {
        "schema_version": REPORT_SCHEMA_VERSION,
        "plugin_version": version.PLUGIN_VERSION,
        "module_api_version": API_VERSION,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "engine_version": _engine_version(),
        "diagnostic_type": "mesh_quality",
        "read_only": True,
        "summary": _report_summary(rows, scan_summary),
        "results": [mesh_quality_row_to_dict(row) for row in rows],
    }
    json_bytes = json.dumps(
        payload, ensure_ascii=False, indent=2, allow_nan=False
    ).encode("utf-8")
    _atomic_write(json_path, json_bytes)

    text = io.StringIO(newline="")
    writer = csv.DictWriter(text, fieldnames=_REPORT_COLUMNS, extrasaction="ignore")
    writer.writeheader()
    for row in rows:
        flat = mesh_quality_row_to_dict(row)
        flat["world_scale"] = " x ".join(
            "{:.6g}".format(value) for value in row.world_scale
        )
        for name in (
            "actor_paths",
            "actor_labels",
            "component_paths",
            "component_names",
            "error_triangle_ids",
            "triangle_ids",
        ):
            flat[name] = " | ".join(str(value) for value in flat.get(name, []))
        for name in (
            "local_bounds_min",
            "local_bounds_max",
            "sample_vertices",
            "sample_face_normals",
            "details",
        ):
            flat[name] = json.dumps(flat.get(name), ensure_ascii=False, allow_nan=False)
        writer.writerow(flat)
    _atomic_write(csv_path, ("\ufeff" + text.getvalue()).encode("utf-8"))
    return json_path, csv_path


# Short aliases keep the eventual UI call sites readable and provide a stable
# compatibility layer if this module is split into normal/UV files later.
scan_selected = scan_selected_mesh_quality
select_rows = select_mesh_quality_row_actors
sync_assets = sync_mesh_quality_assets
locate_row = locate_mesh_quality_row
export_report = export_mesh_quality_report


__all__ = [
    "API_VERSION",
    "REPORT_SCHEMA_VERSION",
    "NativeAuditUnavailableError",
    "MeshQualityScanCancelled",
    "MeshComponentUse",
    "MeshAuditGroup",
    "MeshQualityOptions",
    "MeshQualityRow",
    "affected_element_unit",
    "format_affected_elements",
    "native_audit_available",
    "options_for_preset",
    "build_native_options",
    "collect_selected_mesh_groups",
    "analyze_mesh_groups",
    "scan_selected_mesh_quality",
    "select_mesh_quality_row_actors",
    "sync_mesh_quality_assets",
    "locate_mesh_quality_rows",
    "locate_mesh_quality_row",
    "mesh_quality_row_to_dict",
    "resolve_mesh_quality_report_directory",
    "export_mesh_quality_report",
    "scan_selected",
    "select_rows",
    "sync_assets",
    "locate_row",
    "export_report",
]
