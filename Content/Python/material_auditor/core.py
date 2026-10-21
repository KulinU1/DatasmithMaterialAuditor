# -*- coding: utf-8 -*-
"""Core Unreal Editor operations for Datasmith Material Auditor.

Formal non-glass normalization uses safe duplicated variants. Two-sided changes
and the temporary custom depth check intentionally operate on each unique parent
UMaterial, matching the artist workflow requested for this project.
"""

from __future__ import annotations

import csv
import hashlib
import json
import os
import re
from dataclasses import asdict, dataclass
from datetime import datetime
from typing import Callable, Dict, Iterable, List, Optional, Sequence, Set, Tuple

import unreal

from . import storage, version


TOOL_NAME = "DatasmithMaterialAuditor"
PROCESSED_ROOT = "/Game/MaterialAuditor/Processed"
DEFAULT_POST_DEPTH_MATERIAL = (
    "/Game/Assets/Materials/PostMaterials/M_PostDepth_10.M_PostDepth_10"
)
API_VERSION = version.CORE_API_VERSION
MAX_SCAN_ROOT_COUNT = 64
SCAN_PROGRESS_INTERVAL = 25


_VIEW_REFRESH_HANDLE = None
_PENDING_VIEW_TARGET = ""
_PENDING_VIEW_TICKS = 0
_POST_DEPTH_VOLUME = None


class ScanCancelledError(RuntimeError):
    """Raised when the user cancels a material or UV scan."""


def _log(message: str) -> None:
    unreal.log("[MaterialAuditor] {}".format(message))


def _warn(message: str) -> None:
    unreal.log_warning("[MaterialAuditor] {}".format(message))


def _error(message: str) -> None:
    unreal.log_error("[MaterialAuditor] {}".format(message))


def _saved_dir() -> str:
    return storage.writable_directory()


def session_path() -> str:
    return storage.readable_path("last_session.json")


def user_rules_path() -> str:
    return storage.readable_path("material_rules.json")


def ui_state_path() -> str:
    return storage.readable_path("ui_state.json")


def load_ui_state() -> Dict:
    try:
        return storage.read_json("ui_state.json", default={}) or {}
    except Exception as exc:
        _warn("读取界面设置失败: {}".format(exc))
        return {}


def save_ui_state(state: Dict) -> str:
    return storage.write_json("ui_state.json", state)


def normalize_asset_path(value: str) -> str:
    value = (value or "").strip()
    if not value:
        return ""
    if "'" in value:
        parts = value.split("'")
        if len(parts) >= 2:
            value = parts[1]
    return value.strip('"').strip("'")


def package_path(value: str) -> str:
    value = normalize_asset_path(value)
    return value.split(".", 1)[0]


def object_path(obj) -> str:
    if not obj:
        return ""
    try:
        return obj.get_path_name()
    except Exception:
        return str(obj)


def load_material(path: str):
    path = normalize_asset_path(path)
    if not path:
        return None
    asset = unreal.EditorAssetLibrary.load_asset(path)
    if not asset and "." in path:
        asset = unreal.EditorAssetLibrary.load_asset(package_path(path))
    if not asset:
        return None
    try:
        if isinstance(asset, unreal.MaterialInterface):
            return asset
    except Exception:
        pass
    class_name = asset.get_class().get_name().lower()
    if "material" in class_name and "function" not in class_name:
        return asset
    return None


def sync_content_browser_to_materials(material_paths: Sequence[str]) -> Dict:
    """Locate unique, valid material assets in the Content Browser."""
    located = []
    missing = []
    for path in sorted({normalize_asset_path(value) for value in material_paths if value}):
        material = load_material(path)
        if material:
            located.append(object_path(material))
        else:
            missing.append(path)
    if not located:
        raise RuntimeError("没有可在 Content Browser 中定位的有效材质资产")
    sync = getattr(unreal.EditorAssetLibrary, "sync_browser_to_objects", None)
    if not callable(sync):
        raise RuntimeError("当前 UE 版本没有 Content Browser 定位接口")
    sync(located)
    return {
        "located": len(located),
        "paths": located,
        "missing": missing,
    }


def load_material_function(path: str):
    path = normalize_asset_path(path)
    if not path:
        return None
    asset = unreal.EditorAssetLibrary.load_asset(path)
    if not asset and "." in path:
        asset = unreal.EditorAssetLibrary.load_asset(package_path(path))
    if not asset:
        return None
    function_interface = getattr(unreal, "MaterialFunctionInterface", None)
    if function_interface:
        try:
            if isinstance(asset, function_interface):
                return asset
        except Exception:
            pass
    if "materialfunction" in asset.get_class().get_name().replace("_", "").lower():
        return asset
    return None


def _enum_text(value) -> str:
    text = str(value or "")
    return text.split(".")[-1]


def _blend_mode_token(value) -> str:
    """Return a stable Unreal BlendMode enum token for JSON snapshots."""
    match = re.search(r"BLEND_[A-Z0-9_]+", str(value or "").upper())
    return match.group(0) if match else ""


def _blend_mode_from_token(token: str):
    value = getattr(unreal.BlendMode, str(token or ""), None)
    if value is None:
        raise RuntimeError("无法恢复未知 Blend Mode：{}".format(token))
    return value


def _base_material(material):
    if not material:
        return None
    if isinstance(material, unreal.Material):
        return material
    for method_name in ("get_base_material", "get_material"):
        method = getattr(material, method_name, None)
        if callable(method):
            try:
                result = method()
                if result:
                    return result
            except Exception:
                pass
    try:
        parent = material.get_editor_property("parent")
        if parent:
            return _base_material(parent)
    except Exception:
        pass
    return None


def _material_properties_from_base(material, base) -> Tuple[str, str, str]:
    """Return effective material properties using an already resolved base."""
    blend = "Unknown"
    shading = "Unknown"
    domain = "Unknown"

    try:
        overrides = material.get_editor_property("base_property_overrides")
        if overrides.get_editor_property("override_blend_mode"):
            blend = _enum_text(overrides.get_editor_property("blend_mode"))
        if overrides.get_editor_property("override_shading_model"):
            shading = _enum_text(overrides.get_editor_property("shading_model"))
    except Exception:
        pass

    if base:
        if blend == "Unknown":
            try:
                blend = _enum_text(base.get_editor_property("blend_mode"))
            except Exception:
                pass
        if shading == "Unknown":
            try:
                shading = _enum_text(base.get_editor_property("shading_model"))
            except Exception:
                pass
        try:
            domain = _enum_text(base.get_editor_property("material_domain"))
        except Exception:
            pass
    return blend, shading, domain


def material_properties(material) -> Tuple[str, str, str]:
    """Return effective-ish blend, shading and domain text for classification."""
    return _material_properties_from_base(material, _base_material(material))


def material_two_sided(material) -> bool:
    """Return the final parent Material's two-sided state.

    Instance overrides are intentionally ignored. The workflow modifies each
    unique parent Material once so every child instance benefits.
    """
    if not material:
        return False
    base = _base_material(material)
    if base:
        try:
            return bool(base.get_editor_property("two_sided"))
        except Exception:
            pass
    return False


def base_material_path(material) -> str:
    return object_path(_base_material(material))


def material_normal_connected(material) -> bool:
    """Return whether the final parent UMaterial has a Normal input node."""
    base = _base_material(material)
    return _base_normal_connected(base)


def _base_normal_connected(base) -> bool:
    """Return whether an already resolved parent UMaterial has a Normal input."""
    if not base:
        return False
    try:
        node = unreal.MaterialEditingLibrary.get_material_property_input_node(
            base, unreal.MaterialProperty.MP_NORMAL
        )
        return bool(node)
    except Exception:
        return False


def _buffer_participation_from_properties(
    blend: str, shading: str, domain: str
) -> Tuple[bool, bool, str]:
    """Predict buffer participation from pre-read material properties."""
    blend_upper = blend.upper()
    shading_upper = shading.upper()
    domain_upper = domain.upper()

    is_surface = "SURFACE" in domain_upper or domain == "Unknown"
    is_opaque_path = "OPAQUE" in blend_upper or "MASKED" in blend_upper
    is_unlit = "UNLIT" in shading_upper

    writes_scene_depth = bool(is_surface and is_opaque_path)
    writes_world_normal = bool(is_surface and is_opaque_path and not is_unlit)
    reason = "WorldNormal={}；SceneDepth={}；Blend={}；Shading={}；Domain={}".format(
        "写入" if writes_world_normal else "不写入",
        "写入" if writes_scene_depth else "不写入",
        blend,
        shading,
        domain,
    )
    return writes_world_normal, writes_scene_depth, reason


def buffer_participation(material) -> Tuple[bool, bool, str]:
    """Predict whether a material contributes to World Normal and Scene Depth.

    These flags use the same render-path conditions as the editor buffer views.
    Unlike screenshot color sampling, they map deterministically back to the
    material slot and are not affected by camera framing, sky, or occlusion.
    """
    return _buffer_participation_from_properties(*material_properties(material))


@dataclass
class SlotRecord:
    actor_path: str
    actor_label: str
    component_name: str
    slot_index: int
    slot_name: str
    material_path: str
    material_name: str
    base_material_path: str
    blend_mode: str
    shading_model: str
    material_domain: str
    writes_world_normal: bool
    writes_scene_depth: bool
    two_sided: bool
    normal_connected: bool
    classification: str
    confidence: int
    reason: str


@dataclass
class UVAssetRecord:
    """One unique Static Mesh + component world-scale group."""

    mesh_path: str
    mesh_name: str
    actor_labels: List[str]
    components: List[Dict]
    component_count: int
    local_dimensions: Tuple[float, float, float]
    world_dimensions: Tuple[float, float, float]
    world_scale: Tuple[float, float, float]
    major_area_m2: float
    classification: str
    projection_size: float
    reason: str


_UV_LARGE_ENGLISH_TOKENS = (
    "wall",
    "floor",
    "ceiling",
    "roof",
    "slab",
    "ground",
    "facade",
    "pavement",
)
_UV_LARGE_CJK_TOKENS = ("墙", "地面", "地板", "天花", "屋顶", "屋面", "楼板")


def _actor_subsystem():
    return unreal.get_editor_subsystem(unreal.EditorActorSubsystem)


def selected_level_actors() -> List:
    return list(_actor_subsystem().get_selected_level_actors())


def all_level_actors() -> List:
    return list(_actor_subsystem().get_all_level_actors())


def _actor_label(actor) -> str:
    try:
        return actor.get_actor_label()
    except Exception:
        try:
            return actor.get_name()
        except Exception:
            return object_path(actor)


def capture_selected_scan_roots() -> List[Dict[str, str]]:
    """Capture and validate the current selection as a stable scan scope.

    Descendants are removed when their selected parent is also present. A large
    remaining selection almost always means that a previous result filter was
    selected, so it is rejected before a quadratic scan can begin.
    """
    selected = selected_level_actors()
    if not selected:
        raise RuntimeError(
            "请先在 World Outliner 中选择一个或多个 Datasmith Root Actor。"
        )

    unique_by_path = {}
    for actor in selected:
        path = object_path(actor)
        if path:
            unique_by_path.setdefault(path, actor)
    selected_paths = set(unique_by_path)

    roots = []
    for path, actor in unique_by_path.items():
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
            parent_path = object_path(current)
            if parent_path in seen:
                break
            seen.add(parent_path)
            if parent_path in selected_paths:
                has_selected_parent = True
                break
        if not has_selected_parent:
            roots.append({"path": path, "label": _actor_label(actor)})

    if not roots:
        raise RuntimeError("无法从当前选择中确定有效的 Datasmith Root Actor。")
    if len(roots) > MAX_SCAN_ROOT_COUNT:
        raise RuntimeError(
            "当前选择最终得到 {} 个扫描 Root，超过安全上限 {}。"
            "这通常是因为刚刚选中了筛选结果。请只重新选择最上层 Datasmith Root，"
            "再点击‘读取并锁定当前选中 Root’。".format(
                len(roots), MAX_SCAN_ROOT_COUNT
            )
        )
    return roots


def capture_selected_uv_roots() -> List[Dict[str, str]]:
    """Capture the current Outliner selection for an immediate UV scan.

    UV scanning intentionally does not reuse or replace the material scanner's
    locked roots. Selected descendants are collapsed when their selected
    parent is also present, but there is no material-scan root-count limit:
    artists may deliberately select many independent objects for UV work.
    """
    selected = selected_level_actors()
    if not selected:
        raise RuntimeError("请先在 World Outliner 中选择要扫描 UV 的物体。")

    unique_by_path = {}
    for actor in selected:
        path = object_path(actor)
        if path:
            unique_by_path.setdefault(path, actor)
    selected_paths = set(unique_by_path)

    roots = []
    for path, actor in unique_by_path.items():
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
            parent_path = object_path(current)
            if parent_path in seen:
                break
            seen.add(parent_path)
            if parent_path in selected_paths:
                has_selected_parent = True
                break
        if not has_selected_parent:
            roots.append({"path": path, "label": _actor_label(actor)})

    if not roots:
        raise RuntimeError("无法从当前选择中确定有效的 UV 扫描物体。")
    return roots


def _resolve_scan_scope(
    root_paths: Optional[Sequence[str]] = None,
    enforce_root_limit: bool = True,
) -> Tuple[Set[str], List]:
    if root_paths is None:
        root_paths = [item["path"] for item in capture_selected_scan_roots()]

    requested = []
    seen = set()
    for value in root_paths:
        path = str(value or "").strip()
        if path and path not in seen:
            requested.append(path)
            seen.add(path)
    if not requested:
        raise RuntimeError(
            "扫描范围尚未锁定。请在 World Outliner 选择 Datasmith Root，"
            "再点击‘读取并锁定当前选中 Root’。"
        )
    if enforce_root_limit and len(requested) > MAX_SCAN_ROOT_COUNT:
        raise RuntimeError(
            "锁定的扫描 Root 数量 {} 超过安全上限 {}，请重新锁定范围。".format(
                len(requested), MAX_SCAN_ROOT_COUNT
            )
        )

    actors = all_level_actors()
    available_paths = {object_path(actor) for actor in actors}
    missing = [path for path in requested if path not in available_paths]
    if missing:
        preview = "\n".join(missing[:5])
        raise RuntimeError(
            "锁定的 Datasmith Root 已不存在或关卡已切换，请重新读取扫描范围。\n{}".format(
                preview
            )
        )
    return set(requested), actors


def _report_scan_progress(
    callback: Optional[Callable[[int, int], bool]], done: int, total: int
) -> None:
    if callback and callback(done, total) is False:
        raise ScanCancelledError("扫描已由用户取消。")


def select_record_actors(records: Sequence[SlotRecord]) -> int:
    """Select the unique level actors represented by filtered table records."""
    wanted = {record.actor_path for record in records}
    actors = [actor for actor in all_level_actors() if object_path(actor) in wanted]
    _actor_subsystem().set_selected_level_actors(actors)
    return len(actors)


def _is_under_root(actor, root_paths: Set[str]) -> bool:
    seen = set()
    current = actor
    while current and object_path(current) not in seen:
        current_path = object_path(current)
        if current_path in root_paths:
            return True
        seen.add(current_path)
        try:
            current = current.get_attach_parent_actor()
        except Exception:
            return False
    return False


def _mesh_components(actor) -> Iterable:
    try:
        return list(actor.get_components_by_class(unreal.MeshComponent))
    except Exception:
        try:
            return list(actor.get_components_by_class(unreal.StaticMeshComponent))
        except Exception:
            return []


def _slot_names(component) -> List[str]:
    try:
        return [str(name) for name in component.get_material_slot_names()]
    except Exception:
        return []


def _static_mesh_components(actor) -> Iterable:
    try:
        return list(actor.get_components_by_class(unreal.StaticMeshComponent))
    except Exception:
        return []


def _component_static_mesh(component):
    try:
        return component.get_editor_property("static_mesh")
    except Exception:
        return getattr(component, "static_mesh", None)


def _component_world_scale(component) -> Tuple[float, float, float]:
    try:
        scale = component.get_world_scale()
    except Exception:
        scale = component.get_editor_property("relative_scale3d")
        try:
            owner_scale = component.get_owner().get_actor_scale3d()
            scale = unreal.Vector(
                float(scale.x) * float(owner_scale.x),
                float(scale.y) * float(owner_scale.y),
                float(scale.z) * float(owner_scale.z),
            )
        except Exception:
            pass
    values = (abs(float(scale.x)), abs(float(scale.y)), abs(float(scale.z)))
    return tuple(round(value, 4) for value in values)


def _mesh_local_bounds(mesh) -> Tuple[Tuple[float, float, float], Tuple[float, float, float]]:
    """Return local-space bounds origin and full dimensions in centimeters."""
    try:
        bounds = mesh.get_bounds()
    except Exception:
        bounds = mesh.get_editor_property("extended_bounds")
    origin = bounds.origin
    extent = bounds.box_extent
    return (
        (float(origin.x), float(origin.y), float(origin.z)),
        (
            abs(float(extent.x)) * 2.0,
            abs(float(extent.y)) * 2.0,
            abs(float(extent.z)) * 2.0,
        ),
    )


def _has_large_surface_hint(value: str) -> bool:
    value = (value or "").lower()
    words = set(re.findall(r"[a-z0-9]+", value))
    if any(token in words for token in _UV_LARGE_ENGLISH_TOKENS):
        return True
    return any(token in value for token in _UV_LARGE_CJK_TOKENS)


def scan_uv_assets(
    large_projection_size: float = 200.0,
    small_projection_size: float = 50.0,
    large_area_threshold_m2: float = 4.0,
    root_paths: Optional[Sequence[str]] = None,
    progress_callback: Optional[Callable[[int, int], bool]] = None,
) -> List[UVAssetRecord]:
    """Scan selected Datasmith roots and group Static Mesh uses by world scale.

    The area rule intentionally uses the two largest bound dimensions instead
    of volume, because walls and floors are usually very thin.
    """
    if large_projection_size <= 0 or small_projection_size <= 0:
        raise RuntimeError("盒体投影尺寸必须大于 0。")
    if large_area_threshold_m2 < 0:
        raise RuntimeError("大型表面阈值不能小于 0。")

    if root_paths is None:
        root_paths = [item["path"] for item in capture_selected_uv_roots()]
    scan_root_paths, actors = _resolve_scan_scope(
        root_paths, enforce_root_limit=False
    )
    _log(
        "UV scan started: roots={}, level_actors={}".format(
            len(scan_root_paths), len(actors)
        )
    )
    _report_scan_progress(progress_callback, 0, len(actors))

    groups: Dict[Tuple[str, Tuple[float, float, float]], Dict] = {}
    for actor_index, actor in enumerate(actors, 1):
        if _is_under_root(actor, scan_root_paths):
            actor_label = _actor_label(actor)
            for component in _static_mesh_components(actor):
                mesh = _component_static_mesh(component)
                if not mesh:
                    continue
                mesh_path = object_path(mesh)
                if mesh_path.startswith("/Engine/Transient"):
                    continue
                scale = _component_world_scale(component)
                key = (mesh_path, scale)
                group = groups.setdefault(
                    key,
                    {
                        "mesh": mesh,
                        "mesh_path": mesh_path,
                        "mesh_name": mesh.get_name(),
                        "scale": scale,
                        "actor_labels": [],
                        "components": [],
                    },
                )
                if actor_label not in group["actor_labels"]:
                    group["actor_labels"].append(actor_label)
                group["components"].append(
                    {
                        "actor_path": object_path(actor),
                        "actor_label": actor_label,
                        "component_name": component.get_name(),
                    }
                )
        if actor_index % SCAN_PROGRESS_INTERVAL == 0 or actor_index == len(actors):
            _report_scan_progress(progress_callback, actor_index, len(actors))

    records: List[UVAssetRecord] = []
    for (_mesh_path, _scale), group in groups.items():
        mesh = group["mesh"]
        scale = group["scale"]
        try:
            _origin, local_dimensions = _mesh_local_bounds(mesh)
            world_dimensions = tuple(
                local_dimensions[index] * scale[index] for index in range(3)
            )
            ordered = sorted(world_dimensions, reverse=True)
            major_area_m2 = (ordered[0] * ordered[1]) / 10000.0
            name_blob = " ".join(
                [group["mesh_name"]] + list(group["actor_labels"])
            )
            name_rule = _has_large_surface_hint(name_blob)
            if min(scale) <= 0.0001:
                classification = "skipped"
                projection_size = 0.0
                reason = "组件缩放包含 0，无法进行 UV 尺寸补偿"
            elif name_rule:
                classification = "large"
                projection_size = float(large_projection_size)
                reason = "墙/地面等名称规则"
            elif major_area_m2 >= large_area_threshold_m2:
                classification = "large"
                projection_size = float(large_projection_size)
                reason = "两条最长边面积 {:.2f} m² ≥ {:.2f} m²".format(
                    major_area_m2, large_area_threshold_m2
                )
            else:
                classification = "small"
                projection_size = float(small_projection_size)
                reason = "两条最长边面积 {:.2f} m² < {:.2f} m²".format(
                    major_area_m2, large_area_threshold_m2
                )
        except Exception as exc:
            local_dimensions = (0.0, 0.0, 0.0)
            world_dimensions = (0.0, 0.0, 0.0)
            major_area_m2 = 0.0
            classification = "skipped"
            projection_size = 0.0
            reason = "读取网格边界失败：{}".format(exc)

        records.append(
            UVAssetRecord(
                mesh_path=group["mesh_path"],
                mesh_name=group["mesh_name"],
                actor_labels=sorted(group["actor_labels"]),
                components=group["components"],
                component_count=len(group["components"]),
                local_dimensions=tuple(local_dimensions),
                world_dimensions=tuple(world_dimensions),
                world_scale=tuple(scale),
                major_area_m2=float(major_area_m2),
                classification=classification,
                projection_size=float(projection_size),
                reason=reason,
            )
        )
    records.sort(key=lambda record: (record.classification, record.mesh_name.lower()))
    return records


def select_uv_record_actors(records: Sequence[UVAssetRecord]) -> int:
    wanted = {
        item.get("actor_path", "")
        for record in records
        for item in record.components
    }
    actors = [actor for actor in all_level_actors() if object_path(actor) in wanted]
    _actor_subsystem().set_selected_level_actors(actors)
    return len(actors)


def load_rules(default_rules_file: str) -> Dict:
    with open(default_rules_file, "r", encoding="utf-8") as handle:
        rules = json.load(handle)
    override_path = user_rules_path()
    if os.path.exists(override_path):
        try:
            with open(override_path, "r", encoding="utf-8") as handle:
                overrides = json.load(handle)
            for key, value in overrides.items():
                rules[key] = value
        except Exception as exc:
            _warn("读取用户规则失败: {}".format(exc))
    return rules


def save_confirmed_rules(records: Sequence[SlotRecord], default_rules_file: str) -> str:
    rules = load_rules(default_rules_file)
    keys = {
        "glass": "glass_material_paths",
        "non_glass": "non_glass_material_paths",
        "excluded": "excluded_material_paths",
    }
    for record in records:
        for list_key in keys.values():
            rules.setdefault(list_key, [])
            if record.material_path in rules[list_key]:
                rules[list_key].remove(record.material_path)
        key = keys.get(record.classification)
        if key and record.material_path and record.material_path not in rules[key]:
            rules[key].append(record.material_path)
    return storage.write_json("material_rules.json", rules)


def _classify_from_properties(
    blend: str, shading: str, domain: str
) -> Tuple[str, int, str, bool, bool]:
    """Classify from already cached material properties."""
    writes_normal, writes_depth, reason = _buffer_participation_from_properties(
        blend, shading, domain
    )
    domain_upper = domain.upper()
    if "DECAL" in domain_upper or "POST" in domain_upper or "VOLUME" in domain_upper:
        return "excluded", 100, reason, writes_normal, writes_depth

    if not writes_normal and not writes_depth:
        translucent_path = any(
            token in blend.upper()
            for token in ("TRANSLUCENT", "ADDITIVE", "MODULATE", "ALPHA")
        )
        confidence = 95 if translucent_path else 75
        return "glass", confidence, reason, writes_normal, writes_depth

    if writes_normal and writes_depth:
        return "non_glass", 100, reason, writes_normal, writes_depth

    # A surface that writes only one expected buffer is a render-path anomaly,
    # not automatically glass. Keep it visible for review and skip modification.
    return "issue", 100, reason, writes_normal, writes_depth


def classify(material) -> Tuple[str, int, str, bool, bool]:
    """Classify exclusively from World Normal / Scene Depth participation."""
    return _classify_from_properties(*material_properties(material))


def scan_selected_roots(
    default_rules_file: str,
    root_paths: Optional[Sequence[str]] = None,
    progress_callback: Optional[Callable[[int, int], bool]] = None,
) -> List[SlotRecord]:
    # Retained for compatibility; material names and keyword rules are no
    # longer used for classification.
    _ = default_rules_file
    scan_root_paths, actors = _resolve_scan_scope(root_paths)
    records: List[SlotRecord] = []
    material_cache: Dict[str, Dict] = {}
    normal_cache: Dict[str, bool] = {}
    two_sided_cache: Dict[str, bool] = {}

    _log(
        "Material scan started: roots={}, level_actors={}".format(
            len(scan_root_paths), len(actors)
        )
    )
    _report_scan_progress(progress_callback, 0, len(actors))

    for actor_index, actor in enumerate(actors, 1):
        if _is_under_root(actor, scan_root_paths):
            actor_label = _actor_label(actor)
            actor_path = object_path(actor)
            for component in _mesh_components(actor):
                names = _slot_names(component)
                try:
                    count = component.get_num_materials()
                except Exception:
                    count = len(names)
                for index in range(count):
                    material = component.get_material(index)
                    if not material:
                        continue
                    material_path = object_path(material)
                    cached = material_cache.get(material_path)
                    if cached is None:
                        base = _base_material(material)
                        base_path = object_path(base)
                        blend, shading, domain = _material_properties_from_base(
                            material, base
                        )
                        (
                            classification,
                            confidence,
                            reason,
                            writes_normal,
                            writes_depth,
                        ) = _classify_from_properties(blend, shading, domain)
                        if base_path not in normal_cache:
                            normal_cache[base_path] = _base_normal_connected(base)
                        if base_path not in two_sided_cache:
                            try:
                                two_sided_cache[base_path] = bool(
                                    base and base.get_editor_property("two_sided")
                                )
                            except Exception:
                                two_sided_cache[base_path] = False
                        cached = {
                            "material_name": material.get_name(),
                            "base_material_path": base_path,
                            "blend_mode": blend,
                            "shading_model": shading,
                            "material_domain": domain,
                            "writes_world_normal": writes_normal,
                            "writes_scene_depth": writes_depth,
                            "two_sided": two_sided_cache[base_path],
                            "normal_connected": normal_cache[base_path],
                            "classification": classification,
                            "confidence": confidence,
                            "reason": reason,
                        }
                        material_cache[material_path] = cached
                    slot_name = (
                        names[index] if index < len(names) else "Slot_{}".format(index)
                    )
                    records.append(
                        SlotRecord(
                            actor_path=actor_path,
                            actor_label=actor_label,
                            component_name=component.get_name(),
                            slot_index=index,
                            slot_name=slot_name,
                            material_path=material_path,
                            **cached
                        )
                    )
        if actor_index % SCAN_PROGRESS_INTERVAL == 0 or actor_index == len(actors):
            _report_scan_progress(progress_callback, actor_index, len(actors))
    _log(
        "Material scan finished: slots={}, unique_materials={}".format(
            len(records), len(material_cache)
        )
    )
    return records


def _find_actor(path: str):
    for actor in all_level_actors():
        if object_path(actor) == path:
            return actor
    return None


def _find_component(actor, name: str):
    if not actor:
        return None
    for component in actor.get_components_by_class(unreal.ActorComponent):
        if component.get_name() == name:
            return component
    return None


class _SlotResolver:
    """Resolve slot records without repeatedly scanning the whole level.

    Large Datasmith scenes can contain tens of thousands of slot records. The
    previous implementation called ``get_all_level_actors`` for every record,
    turning each processing pass into O(slot_count * actor_count). Build the
    actor index once and cache component lookups for the lifetime of one task.
    """

    def __init__(self, actors: Optional[Sequence] = None):
        source = list(actors) if actors is not None else all_level_actors()
        self.actor_cache = {object_path(actor): actor for actor in source}
        self.component_cache: Dict[Tuple[str, str], object] = {}

    def component(self, record: SlotRecord):
        key = (record.actor_path, record.component_name)
        if key not in self.component_cache:
            actor = self.actor_cache.get(record.actor_path)
            self.component_cache[key] = _find_component(actor, record.component_name)
        return self.component_cache[key]

    def current_material(self, record: SlotRecord):
        component = self.component(record)
        if not component:
            return None, None
        return component, component.get_material(record.slot_index)


def _current_slot_material(
    record: SlotRecord, resolver: Optional[_SlotResolver] = None
):
    resolver = resolver or _SlotResolver()
    return resolver.current_material(record)


def _snapshot(
    records: Sequence[SlotRecord], resolver: Optional[_SlotResolver] = None
) -> List[Dict]:
    resolver = resolver or _SlotResolver()
    result = []
    seen = set()
    for record in records:
        key = (record.actor_path, record.component_name, record.slot_index)
        if key in seen:
            continue
        seen.add(key)
        component, material = _current_slot_material(record, resolver)
        if component and material:
            result.append(
                {
                    "actor_path": record.actor_path,
                    "component_name": record.component_name,
                    "slot_index": record.slot_index,
                    "material_path": object_path(material),
                }
            )
    return result


def _read_session() -> Dict:
    return storage.read_json("last_session.json", default={}) or {}


def _write_session(data: Dict) -> None:
    data["updated_at"] = datetime.now().isoformat(timespec="seconds")
    # This file can contain tens of thousands of slot snapshots. Compact JSON
    # materially reduces main-thread disk I/O while retaining the same schema.
    storage.write_json("last_session.json", data, compact=True)


def _set_slots(snapshot: Sequence[Dict]) -> Tuple[int, List[str]]:
    restored = 0
    errors = []
    actor_cache = {object_path(actor): actor for actor in all_level_actors()}
    component_cache = {}
    material_cache = {}
    for item in snapshot:
        try:
            actor = actor_cache.get(item["actor_path"])
            component_key = (item["actor_path"], item["component_name"])
            if component_key not in component_cache:
                component_cache[component_key] = _find_component(
                    actor, item["component_name"]
                )
            component = component_cache[component_key]
            material_path = item["material_path"]
            if material_path not in material_cache:
                material_cache[material_path] = load_material(material_path)
            material = material_cache[material_path]
            if not component or not material:
                raise RuntimeError("Actor、组件或材质不存在")
            component.modify()
            component.set_material(int(item["slot_index"]), material)
            restored += 1
        except Exception as exc:
            errors.append("{} / {} / {}: {}".format(
                item.get("actor_path"), item.get("component_name"), item.get("slot_index"), exc
            ))
    return restored, errors


def _sanitize_name(value: str) -> str:
    value = re.sub(r"[^A-Za-z0-9_]+", "_", value or "Material")
    return value.strip("_") or "Material"


def _level_name() -> str:
    try:
        subsystem = unreal.get_editor_subsystem(unreal.UnrealEditorSubsystem)
        world = subsystem.get_editor_world()
        return _sanitize_name(world.get_name())
    except Exception:
        return "CurrentLevel"


def _meets_variant_requirements(
    material, force_surface: bool, force_two_sided: bool
) -> bool:
    blend, shading, _domain = material_properties(material)
    surface_ok = (
        not force_surface
        or ("OPAQUE" in blend.upper() and "DEFAULT_LIT" in shading.upper())
    )
    two_sided_ok = not force_two_sided or material_two_sided(material)
    return surface_ok and two_sided_ok


def _material_variant(
    source,
    cache: Dict[Tuple[str, bool, bool], object],
    force_surface: bool,
    force_two_sided: bool,
):
    source_object_path = object_path(source)
    cache_key = (source_object_path, force_surface, force_two_sided)
    if cache_key in cache:
        return cache[cache_key]
    if _meets_variant_requirements(source, force_surface, force_two_sided):
        cache[cache_key] = source
        return source

    source_pkg = package_path(source_object_path)
    variant_label = "Opaque" if force_surface else "OriginalSurface"
    if force_two_sided:
        variant_label += "_TwoSided"
    digest_source = "{}|{}|{}".format(
        source_object_path, force_surface, force_two_sided
    )
    digest = hashlib.sha1(digest_source.encode("utf-8")).hexdigest()[:8]
    destination = "{}/{}/Materials/{}_{}_{}".format(
        PROCESSED_ROOT,
        _level_name(),
        _sanitize_name(source.get_name()),
        variant_label,
        digest,
    )
    if unreal.EditorAssetLibrary.does_asset_exist(destination):
        duplicate = unreal.EditorAssetLibrary.load_asset(destination)
    else:
        duplicate = unreal.EditorAssetLibrary.duplicate_asset(source_pkg, destination)
    if not duplicate:
        raise RuntimeError("无法复制材质 {}".format(source_object_path))

    if isinstance(duplicate, unreal.Material):
        if force_surface:
            duplicate.set_editor_property("blend_mode", unreal.BlendMode.BLEND_OPAQUE)
            duplicate.set_editor_property(
                "shading_model", unreal.MaterialShadingModel.MSM_DEFAULT_LIT
            )
        if force_two_sided:
            duplicate.set_editor_property("two_sided", True)
        compile_errors = unreal.MaterialEditingLibrary.recompile_material(duplicate)
        if compile_errors:
            _warn("材质编译信息 {}: {}".format(destination, compile_errors))
    else:
        overrides = duplicate.get_editor_property("base_property_overrides")
        if force_surface:
            overrides.set_editor_property("override_blend_mode", True)
            overrides.set_editor_property("blend_mode", unreal.BlendMode.BLEND_OPAQUE)
            overrides.set_editor_property("override_shading_model", True)
            overrides.set_editor_property(
                "shading_model", unreal.MaterialShadingModel.MSM_DEFAULT_LIT
            )
        if force_two_sided:
            overrides.set_editor_property("override_two_sided", True)
            overrides.set_editor_property("two_sided", True)
        duplicate.set_editor_property("base_property_overrides", overrides)
        unreal.MaterialEditingLibrary.update_material_instance(duplicate)

    unreal.EditorAssetLibrary.save_loaded_asset(duplicate, only_if_is_dirty=False)
    cache[cache_key] = duplicate
    return duplicate


def _start_processing_session(
    records: Sequence[SlotRecord],
    resolver: Optional[_SlotResolver] = None,
    snapshot_slots: bool = True,
) -> Dict:
    session = _read_session()
    session["original_snapshot"] = (
        _snapshot(records, resolver) if snapshot_slots else []
    )
    session["base_material_snapshot"] = []
    session["debug_snapshot"] = []
    session["glass_base_blend_snapshot"] = []
    session["post_depth_preview"] = {}
    session["glass_slots"] = []
    _write_session(session)
    return session


def _materials_from_records(
    records: Sequence[SlotRecord], resolver: Optional[_SlotResolver] = None
) -> List:
    resolver = resolver or _SlotResolver()
    materials = []
    seen = set()
    scanned_paths = set()
    for record in records:
        # Records sharing a scanned material path resolve to the same current
        # material before a formal processing pass. Resolve one representative
        # instead of crossing the Python/UE boundary for every repeated slot.
        if record.material_path in scanned_paths:
            continue
        scanned_paths.add(record.material_path)
        _component, material = _current_slot_material(record, resolver)
        path = object_path(material)
        if material and path not in seen:
            seen.add(path)
            materials.append(material)
    return materials


def _enable_parent_materials_two_sided(
    materials: Sequence, session: Optional[Dict] = None
) -> Dict:
    bases = {}
    for material in materials:
        base = _base_material(material)
        if base:
            bases[object_path(base)] = base

    owns_session = session is None
    session = session if session is not None else _read_session()
    snapshot_by_path = {
        item["material_path"]: item
        for item in session.get("base_material_snapshot", [])
    }
    changed = 0
    already_enabled = 0
    errors: List[str] = []
    for path, base in bases.items():
        try:
            was_two_sided = bool(base.get_editor_property("two_sided"))
            snapshot_by_path.setdefault(
                path,
                {"material_path": path, "two_sided": was_two_sided},
            )
            if was_two_sided:
                already_enabled += 1
                continue
            base.modify()
            base.set_editor_property("two_sided", True)
            compile_errors = unreal.MaterialEditingLibrary.recompile_material(base)
            if compile_errors:
                _warn("母材质编译信息 {}: {}".format(path, compile_errors))
            if not unreal.EditorAssetLibrary.save_loaded_asset(
                base, only_if_is_dirty=False
            ):
                raise RuntimeError("UE 无法保存修改后的母材质")
            changed += 1
        except Exception as exc:
            errors.append("{}: {}".format(path, exc))
    session["base_material_snapshot"] = list(snapshot_by_path.values())
    if owns_session:
        _write_session(session)
    return {
        "changed": changed,
        "already_enabled": already_enabled,
        "unique_bases": len(bases),
        "errors": errors,
    }


def _restore_parent_materials(snapshot: Sequence[Dict]) -> Tuple[int, List[str]]:
    restored = 0
    errors: List[str] = []
    for item in snapshot:
        try:
            material = load_material(item["material_path"])
            base = _base_material(material)
            if not base:
                raise RuntimeError("找不到母材质")
            base.modify()
            base.set_editor_property("two_sided", bool(item["two_sided"]))
            compile_errors = unreal.MaterialEditingLibrary.recompile_material(base)
            if compile_errors:
                _warn("恢复母材质编译信息 {}: {}".format(item["material_path"], compile_errors))
            unreal.EditorAssetLibrary.save_loaded_asset(base, only_if_is_dirty=False)
            restored += 1
        except Exception as exc:
            errors.append("{}: {}".format(item.get("material_path"), exc))
    return restored, errors


def apply_processing(
    records: Sequence[SlotRecord],
    glass_material_path: str,
    enable_two_sided: bool = True,
) -> Dict:
    actionable = [r for r in records if r.classification in ("glass", "non_glass")]
    if not actionable:
        raise RuntimeError("当前列表中没有可处理的玻璃或非玻璃材质。")
    has_glass = any(record.classification == "glass" for record in actionable)
    glass_material = load_material(glass_material_path) if has_glass else None
    if has_glass and not glass_material:
        raise RuntimeError("请选择有效的正式玻璃材质。")

    # Never lose a temporary Scene Depth snapshot if the user starts formal
    # processing before explicitly returning to Lit.
    _restore_depth_material_state()

    resolver = _SlotResolver()
    session = _start_processing_session(actionable, resolver)
    session["glass_material_path"] = object_path(glass_material)

    variant_cache: Dict[Tuple[str, bool, bool], object] = {}
    changed = 0
    glass_count = 0
    opaque_count = 0
    errors: List[str] = []
    glass_slots: List[Dict] = []

    two_sided_result = {
        "changed": 0,
        "already_enabled": 0,
        "unique_bases": 0,
        "errors": [],
    }
    if enable_two_sided:
        materials = _materials_from_records(actionable, resolver)
        if glass_material:
            materials.append(glass_material)
        two_sided_result = _enable_parent_materials_two_sided(materials, session)
        errors.extend(two_sided_result["errors"])

    for record in actionable:
        try:
            component, current = _current_slot_material(record, resolver)
            if not component or not current:
                raise RuntimeError("找不到组件或当前材质")
            if record.classification == "glass":
                target = glass_material
                glass_count += 1
                glass_slots.append(
                    {
                        "actor_path": record.actor_path,
                        "component_name": record.component_name,
                        "slot_index": record.slot_index,
                    }
                )
            else:
                target = _material_variant(
                    current,
                    variant_cache,
                    force_surface=True,
                    force_two_sided=False,
                )
                opaque_count += 1
            if object_path(current) != object_path(target):
                component.modify()
                component.set_material(record.slot_index, target)
                changed += 1
        except Exception as exc:
            errors.append("{} / {} / {}: {}".format(
                record.actor_label, record.component_name, record.slot_index, exc
            ))

    session["glass_slots"] = glass_slots
    session["processed"] = True
    _write_session(session)
    return {
        "changed": changed,
        "glass": glass_count,
        "opaque": opaque_count,
        "two_sided": two_sided_result["changed"],
        "unique_bases": two_sided_result["unique_bases"],
        "errors": errors,
        "session_path": session_path(),
    }


def enable_two_sided(records: Sequence[SlotRecord]) -> Dict:
    actionable = [record for record in records if record.classification != "excluded"]
    if not actionable:
        raise RuntimeError("当前列表中没有可处理材质。")
    _restore_depth_material_state()
    resolver = _SlotResolver()
    session = _start_processing_session(
        actionable, resolver, snapshot_slots=False
    )
    result = _enable_parent_materials_two_sided(
        _materials_from_records(actionable, resolver), session
    )
    session["processed"] = True
    _write_session(session)
    return result


def connect_missing_normal_function(
    records: Sequence[SlotRecord], material_function_path: str
) -> Dict:
    """Connect a selected function to empty Normal inputs on unique parents."""
    material_function = load_material_function(material_function_path)
    if not material_function:
        raise RuntimeError("请选择有效的 Material Function 或 Material Function Instance。")

    _restore_depth_material_state()
    resolver = _SlotResolver()
    bases: Dict[str, object] = {}
    for record in records:
        if record.classification == "excluded":
            continue
        _component, current = _current_slot_material(record, resolver)
        base = _base_material(current)
        if not base:
            continue
        try:
            domain = _enum_text(base.get_editor_property("material_domain"))
        except Exception:
            domain = "Unknown"
        if any(token in domain.upper() for token in ("POST", "DECAL", "VOLUME")):
            continue
        bases[object_path(base)] = base

    if not bases:
        raise RuntimeError("当前扫描范围内没有可处理的表面母材质。")

    changed = 0
    already_connected = 0
    errors: List[str] = []
    changed_materials: List[str] = []
    library = unreal.MaterialEditingLibrary
    for path, base in bases.items():
        call = None
        try:
            if material_normal_connected(base):
                already_connected += 1
                continue
            base.modify()
            call = library.create_material_expression(
                base,
                unreal.MaterialExpressionMaterialFunctionCall,
                -400,
                0,
            )
            if not call:
                raise RuntimeError("无法创建 Material Function Call 节点")
            if not call.set_material_function(material_function):
                raise RuntimeError("所选 Material Function 无法赋给函数调用节点")
            if not library.connect_material_property(
                call,
                "",
                unreal.MaterialProperty.MP_NORMAL,
            ):
                raise RuntimeError("所选函数的第一个输出无法连接到 Normal")
            if not material_normal_connected(base):
                raise RuntimeError("Normal 连接后复核失败")
            compile_errors = library.recompile_material(base)
            if compile_errors:
                _warn("补法线函数编译信息 {}: {}".format(path, compile_errors))
            if not unreal.EditorAssetLibrary.save_loaded_asset(
                base, only_if_is_dirty=False
            ):
                raise RuntimeError("UE 无法保存补全 Normal 后的母材质")
            changed += 1
            changed_materials.append(path)
        except Exception as exc:
            # Remove the node created by this run on any failure so the graph is
            # not left half-modified or connected-but-unsaved.
            if call:
                try:
                    library.delete_material_expression(base, call)
                    library.recompile_material(base)
                except Exception:
                    pass
            errors.append("{}: {}".format(path, exc))

    return {
        "unique_bases": len(bases),
        "changed": changed,
        "already_connected": already_connected,
        "errors": errors,
        "function_path": object_path(material_function),
        "changed_materials": changed_materials,
    }


def _editor_world():
    subsystem = unreal.get_editor_subsystem(unreal.UnrealEditorSubsystem)
    return subsystem.get_editor_world()


def _console(command: str) -> None:
    unreal.SystemLibrary.execute_console_command(_editor_world(), command)


def _invalidate_viewports() -> None:
    """Best-effort redraw for both older and newer UE Python APIs."""
    try:
        invalidate = getattr(unreal.EditorLevelLibrary, "editor_invalidate_viewports", None)
        if callable(invalidate):
            invalidate()
    except Exception:
        pass


def _apply_view_target(target: str) -> Dict:
    """Set the real Level Editor viewport mode; never report false success."""
    automation = getattr(unreal, "AutomationLibrary", None)
    if automation is None:
        raise RuntimeError("UE AutomationLibrary 未加载，无法控制 Level Viewport。")

    if target == "Lit":
        automation.set_editor_viewport_view_mode(unreal.ViewModeIndex.VMI_LIT)
    else:
        # This API controls all viewports in the first Level Editor and also
        # selects the requested buffer. A Python string is converted to FName
        # by the UE binding; constructing unreal.Name is not portable.
        automation.set_editor_viewport_view_mode(
            unreal.ViewModeIndex.VMI_VISUALIZE_BUFFER
        )
        automation.set_editor_viewport_visualize_buffer(target)
    _invalidate_viewports()
    try:
        actual_mode = _enum_text(
            automation.get_editor_active_viewport_view_mode()
        )
    except Exception:
        actual_mode = "Unknown"
    actual_buffer = ""
    if target != "Lit":
        try:
            actual_buffer = unreal.SystemLibrary.get_console_variable_string_value(
                "r.BufferVisualizationTarget"
            )
        except Exception:
            pass
        if actual_buffer and actual_buffer.lower() != target.lower():
            raise RuntimeError(
                "Level Viewport 缓冲目标设置失败：请求 {}，实际 {}".format(
                    target, actual_buffer
                )
            )
    return {
        "target": target,
        "view_mode": actual_mode,
        "buffer_target": actual_buffer,
    }


def _deferred_view_refresh(_delta_seconds) -> None:
    global _VIEW_REFRESH_HANDLE, _PENDING_VIEW_TARGET, _PENDING_VIEW_TICKS
    _PENDING_VIEW_TICKS += 1
    if _PENDING_VIEW_TICKS < 2:
        return
    target = _PENDING_VIEW_TARGET
    handle = _VIEW_REFRESH_HANDLE
    _VIEW_REFRESH_HANDLE = None
    _PENDING_VIEW_TARGET = ""
    _PENDING_VIEW_TICKS = 0
    try:
        if target:
            _apply_view_target(target)
    except Exception as exc:
        _error("延迟刷新视口失败：{}".format(exc))
    finally:
        if handle is not None:
            try:
                unreal.unregister_slate_post_tick_callback(handle)
            except Exception:
                pass


def _switch_view(target: str) -> Dict:
    global _VIEW_REFRESH_HANDLE, _PENDING_VIEW_TARGET, _PENDING_VIEW_TICKS
    if _VIEW_REFRESH_HANDLE is not None:
        try:
            unreal.unregister_slate_post_tick_callback(_VIEW_REFRESH_HANDLE)
        except Exception:
            pass
        _VIEW_REFRESH_HANDLE = None
    result = _apply_view_target(target)
    _PENDING_VIEW_TARGET = target
    _PENDING_VIEW_TICKS = 0
    try:
        _VIEW_REFRESH_HANDLE = unreal.register_slate_post_tick_callback(
            _deferred_view_refresh
        )
    except Exception:
        _VIEW_REFRESH_HANDLE = None
    return result


def enter_world_normal() -> Dict:
    # World Normal should show the normal render path, not the temporary
    # opaque glass/post-process state used only by the custom depth preview.
    _restore_depth_material_state()
    return _switch_view("WorldNormal")


def _glass_base_materials(
    scan_records: Sequence[SlotRecord], resolver: Optional[_SlotResolver] = None
) -> Dict[str, object]:
    resolver = resolver or _SlotResolver()
    bases: Dict[str, object] = {}
    for record in scan_records:
        if record.classification != "glass":
            continue
        _component, current = _current_slot_material(record, resolver)
        base = _base_material(current)
        if base:
            bases[object_path(base)] = base
    return bases


def _make_glass_bases_opaque(scan_records: Sequence[SlotRecord]) -> Dict:
    bases = _glass_base_materials(scan_records)
    if not bases:
        raise RuntimeError("法线/深度规则没有检测到玻璃槽。请先扫描选中的 Root。")

    session = _read_session()
    snapshot_by_path = {
        item["material_path"]: item
        for item in session.get("glass_base_blend_snapshot", [])
    }
    changed = 0
    already_opaque = 0
    errors: List[str] = []
    for path, base in bases.items():
        try:
            current_mode = base.get_editor_property("blend_mode")
            token = _blend_mode_token(current_mode)
            if not token:
                raise RuntimeError("无法读取母材质 Blend Mode")
            snapshot_by_path.setdefault(
                path,
                {"material_path": path, "blend_mode": token},
            )
            if token == "BLEND_OPAQUE":
                already_opaque += 1
                continue
            base.modify()
            base.set_editor_property("blend_mode", unreal.BlendMode.BLEND_OPAQUE)
            compile_errors = unreal.MaterialEditingLibrary.recompile_material(base)
            if compile_errors:
                _warn("Scene Depth 临时编译信息 {}: {}".format(path, compile_errors))
            changed += 1
        except Exception as exc:
            errors.append("{}: {}".format(path, exc))
    session["glass_base_blend_snapshot"] = list(snapshot_by_path.values())
    _write_session(session)
    return {
        "changed_bases": changed,
        "already_opaque": already_opaque,
        "unique_bases": len(bases),
        "errors": errors,
    }


def _restore_glass_base_blends(snapshot: Sequence[Dict]) -> Tuple[int, List[str]]:
    restored = 0
    errors: List[str] = []
    for item in snapshot:
        path = item.get("material_path", "")
        try:
            material = load_material(path)
            base = _base_material(material)
            if not base:
                raise RuntimeError("找不到玻璃母材质")
            base.modify()
            base.set_editor_property(
                "blend_mode", _blend_mode_from_token(item.get("blend_mode", ""))
            )
            compile_errors = unreal.MaterialEditingLibrary.recompile_material(base)
            if compile_errors:
                _warn("恢复玻璃母材质编译信息 {}: {}".format(path, compile_errors))
            restored += 1
        except Exception as exc:
            errors.append("{}: {}".format(path, exc))
    return restored, errors


def _validate_post_depth_material(material_path: str):
    material = load_material(material_path)
    if not material:
        raise RuntimeError("请选择有效的场景深度后期材质或材质实例。")
    _blend, _shading, domain = material_properties(material)
    if "POST_PROCESS" not in domain.upper() and "POSTPROCESS" not in domain.upper():
        raise RuntimeError(
            "{} 不是 Post Process 域材质（当前 Domain：{}）。".format(
                object_path(material), domain
            )
        )
    return material


def _create_post_depth_preview(material) -> Dict:
    """Create a transient unbound PPV containing only the depth material.

    A dedicated transient volume avoids changing, saving, or reordering any of
    the user's existing Post Process Volumes and blendables.
    """
    global _POST_DEPTH_VOLUME
    actors = _actor_subsystem()
    volume = actors.spawn_actor_from_class(
        unreal.PostProcessVolume,
        unreal.Vector(0.0, 0.0, 0.0),
        unreal.Rotator(0.0, 0.0, 0.0),
        transient=True,
    )
    if not volume:
        raise RuntimeError("无法创建临时 Post Process Volume。")
    try:
        volume.set_actor_label("MaterialAuditor_DepthPreview")
        volume.set_editor_property("enabled", True)
        volume.set_editor_property("unbound", True)
        volume.set_editor_property("blend_weight", 1.0)
        volume.set_editor_property("priority", 1000000.0)
        volume.add_or_update_blendable(material, 1.0)
        _POST_DEPTH_VOLUME = volume
        preview = {
            "volume_path": object_path(volume),
            "material_path": object_path(material),
            "transient": True,
        }
        session = _read_session()
        session["post_depth_preview"] = preview
        _write_session(session)
        _invalidate_viewports()
        return preview
    except Exception:
        try:
            actors.destroy_actor(volume)
        except Exception:
            pass
        _POST_DEPTH_VOLUME = None
        raise


def _restore_post_depth_preview(preview: Dict) -> Tuple[int, List[str]]:
    global _POST_DEPTH_VOLUME
    errors: List[str] = []
    if not preview and not _POST_DEPTH_VOLUME:
        return 0, errors

    volume = _POST_DEPTH_VOLUME
    if not volume:
        volume = _find_actor(preview.get("volume_path", ""))
    if not volume:
        _POST_DEPTH_VOLUME = None
        return 0, errors
    try:
        if not _actor_subsystem().destroy_actor(volume):
            raise RuntimeError("UE 未能销毁临时 Post Process Volume")
        _POST_DEPTH_VOLUME = None
        _invalidate_viewports()
        return 1, errors
    except Exception as exc:
        errors.append("恢复场景深度后期预览失败：{}".format(exc))
        return 0, errors


def enter_scene_depth(
    scan_records: Sequence[SlotRecord],
    post_depth_material_path: str = DEFAULT_POST_DEPTH_MATERIAL,
) -> Dict:
    """Enter the project's custom grayscale depth preview in Lit mode."""
    # Re-entering the preview must not accumulate transient volumes or replace
    # the original BlendMode snapshot with the already-opaque state.
    _restore_depth_material_state()
    material = _validate_post_depth_material(post_depth_material_path)
    result = _make_glass_bases_opaque(scan_records)
    try:
        result["view"] = _switch_view("Lit")
        result["post_process"] = _create_post_depth_preview(material)
    except Exception:
        _restore_depth_material_state()
        raise
    return result


def _restore_depth_material_state() -> Dict:
    session = _read_session()
    has_persisted_state = bool(
        session.get("post_depth_preview")
        or session.get("debug_snapshot")
        or session.get("glass_base_blend_snapshot")
    )
    if not has_persisted_state and not _POST_DEPTH_VOLUME:
        return {
            "restored": 0,
            "restored_bases": 0,
            "restored_slots": 0,
            "restored_preview": 0,
            "errors": [],
        }
    restored_preview, preview_errors = _restore_post_depth_preview(
        session.get("post_depth_preview", {})
    )
    # Restore old v0.3 slot snapshots once, if a previous session left them.
    restored_slots, errors = _set_slots(session.get("debug_snapshot", []))
    errors.extend(preview_errors)
    restored_bases, base_errors = _restore_glass_base_blends(
        session.get("glass_base_blend_snapshot", [])
    )
    errors.extend(base_errors)
    session["debug_snapshot"] = []
    session["glass_base_blend_snapshot"] = []
    session["post_depth_preview"] = {}
    _write_session(session)
    return {
        "restored": restored_bases,
        "restored_bases": restored_bases,
        "restored_slots": restored_slots,
        "restored_preview": restored_preview,
        "errors": errors,
    }


def restore_debug_and_view() -> Dict:
    result = _restore_depth_material_state()
    result["view"] = _switch_view("Lit")
    return result


def restore_original() -> Dict:
    depth_result = _restore_depth_material_state()
    session = _read_session()
    restored_debug_slots = depth_result["restored_slots"]
    restored_glass_bases = depth_result["restored_bases"]
    errors = list(depth_result["errors"])
    restored, slot_errors = _set_slots(session.get("original_snapshot", []))
    errors.extend(slot_errors)
    restored_bases, base_errors = _restore_parent_materials(
        session.get("base_material_snapshot", [])
    )
    errors.extend(base_errors)
    session["debug_snapshot"] = []
    session["glass_base_blend_snapshot"] = []
    session["post_depth_preview"] = {}
    session["processed"] = False
    _write_session(session)
    _switch_view("Lit")
    return {
        "restored": restored,
        "restored_bases": restored_bases,
        "restored_glass_bases": restored_glass_bases,
        "restored_debug_slots": restored_debug_slots,
        "errors": errors,
    }


def _load_static_mesh(path: str):
    asset = unreal.EditorAssetLibrary.load_asset(normalize_asset_path(path))
    if not asset and "." in path:
        asset = unreal.EditorAssetLibrary.load_asset(package_path(path))
    if not asset:
        return None
    try:
        return asset if isinstance(asset, unreal.StaticMesh) else None
    except Exception:
        return asset if asset.get_class().get_name() == "StaticMesh" else None


def _geometry_outcome_succeeded(outcome) -> bool:
    return "SUCCESS" in str(outcome or "").upper()


def _write_box_projection_to_static_mesh(
    source_mesh,
    target_mesh,
    world_scale: Tuple[float, float, float],
    projection_size: float,
    uv_channel: int,
) -> Tuple[float, float, float]:
    if not hasattr(unreal, "GeometryScript_AssetUtils"):
        raise RuntimeError(
            "Geometry Scripting 插件未加载。请在插件管理中启用 Geometry Scripting 并重启 UE。"
        )
    dynamic_mesh = unreal.DynamicMesh()
    read_options = unreal.GeometryScriptCopyMeshFromAssetOptions()
    read_options.set_editor_properties(
        {
            "apply_build_settings": True,
            "request_tangents": True,
            "ignore_remove_degenerates": False,
            "use_build_scale": True,
        }
    )
    read_lod = unreal.GeometryScriptMeshReadLOD()
    read_lod.set_editor_properties(
        {
            "lod_type": unreal.GeometryScriptLODType.SOURCE_MODEL,
            "lod_index": 0,
        }
    )
    dynamic_mesh, read_outcome = (
        unreal.GeometryScript_AssetUtils.copy_mesh_from_static_mesh_v2(
            source_mesh,
            dynamic_mesh,
            read_options,
            read_lod,
            True,
        )
    )
    if not _geometry_outcome_succeeded(read_outcome):
        raise RuntimeError("读取 Static Mesh Source LOD0 失败：{}".format(read_outcome))

    current_uv_sets = unreal.GeometryScript_MeshQueries.get_num_uv_sets(dynamic_mesh)
    if current_uv_sets <= uv_channel:
        unreal.GeometryScript_UVs.set_num_uv_sets(dynamic_mesh, uv_channel + 1)

    bounds = unreal.GeometryScript_MeshQueries.get_mesh_bounding_box(dynamic_mesh)
    center = unreal.Vector(
        (float(bounds.min.x) + float(bounds.max.x)) * 0.5,
        (float(bounds.min.y) + float(bounds.max.y)) * 0.5,
        (float(bounds.min.z) + float(bounds.max.z)) * 0.5,
    )
    compensated_dimensions = tuple(
        float(projection_size) / max(abs(float(world_scale[index])), 0.0001)
        for index in range(3)
    )
    box_transform = unreal.Transform(
        [float(center.x), float(center.y), float(center.z)],
        [0.0, 0.0, 0.0],
        list(compensated_dimensions),
    )
    selection = unreal.GeometryScriptMeshSelection()
    unreal.GeometryScript_UVs.set_mesh_u_vs_from_box_projection(
        dynamic_mesh,
        int(uv_channel),
        box_transform,
        selection,
        2,
    )

    write_options = unreal.GeometryScriptCopyMeshToAssetOptions()
    write_options.set_editor_properties(
        {
            "enable_recompute_normals": False,
            "enable_recompute_tangents": False,
            "enable_remove_degenerates": False,
            "use_original_vertex_order": False,
            "use_build_scale": True,
            "replace_materials": False,
            "apply_nanite_settings": False,
            # UV overwrite is explicitly non-undoable. Avoid building one large
            # editor transaction for every mesh in a batch.
            "emit_transaction": False,
            "defer_mesh_post_edit_change": False,
        }
    )
    write_lod = unreal.GeometryScriptMeshWriteLOD()
    write_lod.set_editor_properties(
        {"write_hi_res_source": False, "lod_index": 0}
    )
    _dynamic_mesh, write_outcome = (
        unreal.GeometryScript_AssetUtils.copy_mesh_to_static_mesh(
            dynamic_mesh,
            target_mesh,
            write_options,
            write_lod,
            True,
        )
    )
    if not _geometry_outcome_succeeded(write_outcome):
        raise RuntimeError("写入 Static Mesh LOD0 失败：{}".format(write_outcome))
    return compensated_dimensions


def _uv_projection_signature(record: UVAssetRecord) -> Tuple[float, float, float]:
    """Return the local projection dimensions required by one world-scale use."""
    return tuple(
        round(
            float(record.projection_size)
            / max(abs(float(record.world_scale[index])), 0.0001),
            6,
        )
        for index in range(3)
    )


def apply_uv_box_projection(
    records: Sequence[UVAssetRecord],
    uv_channel: int = 0,
    progress_callback: Optional[Callable[[int, int], bool]] = None,
) -> Dict:
    """Rebuild one UV channel and save all modified meshes in one batch."""
    if uv_channel < 0 or uv_channel > 7:
        raise RuntimeError("UV 通道必须在 0 到 7 之间。")
    actionable = [
        record
        for record in records
        if record.classification in ("large", "small")
        and record.projection_size > 0
    ]
    if not actionable:
        raise RuntimeError("当前没有可执行 UV 投影的网格。")

    grouped: Dict[str, Dict] = {}
    conflicts = set()
    for record in actionable:
        signature = _uv_projection_signature(record)
        existing = grouped.get(record.mesh_path)
        if existing and existing["signature"] != signature:
            conflicts.add(record.mesh_path)
            existing["records"].append(record)
            continue
        if existing:
            existing["records"].append(record)
        else:
            grouped[record.mesh_path] = {
                "record": record,
                "records": [record],
                "signature": signature,
            }

    processed_meshes = 0
    affected_components = 0
    modified_assets = []
    modified_meshes = []
    errors = []
    for mesh_path in sorted(conflicts):
        uses = grouped[mesh_path]["records"]
        descriptions = [
            "scale={} size={}".format(record.world_scale, record.projection_size)
            for record in uses
        ]
        errors.append(
            "{}：同一 Static Mesh 被不同缩放/投影尺寸共用，原地写入无法同时满足，已跳过。{}".format(
                mesh_path, "；".join(descriptions)
            )
        )

    work_items = [
        (mesh_path, group)
        for mesh_path, group in grouped.items()
        if mesh_path not in conflicts
    ]
    total = len(work_items)
    attempted = 0
    cancelled = bool(
        progress_callback and progress_callback(0, total) is False
    )

    for mesh_path, group in work_items:
        if cancelled:
            break
        record = group["record"]
        attempted += 1
        try:
            source_package = package_path(mesh_path)
            if not source_package.startswith("/Game/"):
                raise RuntimeError("只允许直接修改 /Game/ 下的项目资产")
            source_mesh = _load_static_mesh(mesh_path)
            if not source_mesh:
                raise RuntimeError("源 Static Mesh 不存在")
            source_mesh.modify()
            _write_box_projection_to_static_mesh(
                source_mesh,
                source_mesh,
                record.world_scale,
                record.projection_size,
                int(uv_channel),
            )
            processed_meshes += 1
            affected_components += sum(
                item.component_count for item in group["records"]
            )
            modified_assets.append(source_package)
            modified_meshes.append(source_mesh)
        except Exception as exc:
            errors.append("{} [{}]：{}".format(record.mesh_name, mesh_path, exc))

        if (
            progress_callback
            and progress_callback(attempted, total) is False
            and attempted < total
        ):
            cancelled = True
            break

    save_succeeded = True
    if modified_meshes:
        try:
            # One API call produces one package-save phase for the whole batch.
            # only_if_is_dirty=False guarantees that every successfully written
            # mesh is persisted, including partial work after cancellation.
            save_succeeded = bool(
                unreal.EditorAssetLibrary.save_loaded_assets(
                    modified_meshes, only_if_is_dirty=False
                )
            )
            if not save_succeeded:
                errors.append(
                    "UV 已写入内存，但批量保存 Static Mesh 失败；资产可能仍处于未保存状态。"
                )
        except Exception as exc:
            save_succeeded = False
            errors.append("批量保存 Static Mesh 失败：{}".format(exc))

    return {
        "processed_meshes": processed_meshes,
        "affected_components": affected_components,
        "modified_assets": sorted(set(modified_assets)),
        "conflicts": len(conflicts),
        "attempted_meshes": attempted,
        "cancelled": cancelled,
        "save_succeeded": save_succeeded,
        "save_batches": 1 if modified_meshes else 0,
        "errors": errors,
    }


def export_uv_report(records: Sequence[UVAssetRecord]) -> Tuple[str, str]:
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    json_path = os.path.join(_saved_dir(), "uv_report_{}.json".format(stamp))
    csv_path = os.path.join(_saved_dir(), "uv_report_{}.csv".format(stamp))
    rows = [asdict(record) for record in records]
    with open(json_path, "w", encoding="utf-8") as handle:
        json.dump(rows, handle, ensure_ascii=False, indent=2)
    flat_rows = []
    for row in rows:
        flat = dict(row)
        flat["actor_labels"] = " | ".join(flat.get("actor_labels", []))
        flat["components"] = json.dumps(
            flat.get("components", []), ensure_ascii=False
        )
        flat["local_dimensions"] = " x ".join(
            "{:.2f}".format(value) for value in flat.get("local_dimensions", [])
        )
        flat["world_dimensions"] = " x ".join(
            "{:.2f}".format(value) for value in flat.get("world_dimensions", [])
        )
        flat["world_scale"] = " x ".join(
            "{:.4f}".format(value) for value in flat.get("world_scale", [])
        )
        flat_rows.append(flat)
    if flat_rows:
        with open(csv_path, "w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=list(flat_rows[0].keys()))
            writer.writeheader()
            writer.writerows(flat_rows)
    return json_path, csv_path


def export_report(records: Sequence[SlotRecord]) -> Tuple[str, str]:
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    json_path = os.path.join(_saved_dir(), "material_report_{}.json".format(stamp))
    csv_path = os.path.join(_saved_dir(), "material_report_{}.csv".format(stamp))
    rows = [asdict(record) for record in records]
    with open(json_path, "w", encoding="utf-8") as handle:
        json.dump(rows, handle, ensure_ascii=False, indent=2)
    if rows:
        with open(csv_path, "w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=list(rows[0].keys()))
            writer.writeheader()
            writer.writerows(rows)
    return json_path, csv_path
