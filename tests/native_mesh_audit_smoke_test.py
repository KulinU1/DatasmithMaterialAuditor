# -*- coding: utf-8 -*-
"""UE 5.6 read-only smoke test for the native Static Mesh audit pipeline.

The test reads the Engine cube and feeds one synthetic signed-scale use group
through the Python result layer. It does not create, modify, save, duplicate,
rename, or delete any actor or asset package.
"""

import os
import sys

import unreal


PLUGIN_PYTHON = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "Content", "Python")
)
if PLUGIN_PYTHON not in sys.path:
    sys.path.insert(0, PLUGIN_PYTHON)

# Avoid accidentally testing a stale Python module after updating the plugin
# inside an already-running editor session.
for module_name in list(sys.modules):
    if module_name == "material_auditor" or module_name.startswith(
        "material_auditor."
    ):
        del sys.modules[module_name]

from material_auditor import mesh_quality


def _read(value, name, default=None):
    getter = getattr(value, "get_editor_property", None)
    if callable(getter):
        try:
            return getter(name)
        except Exception:
            pass
    return getattr(value, name, default)


def _set(value, name, property_value):
    setter = getattr(value, "set_editor_property", None)
    if callable(setter):
        setter(name, property_value)
    else:
        setattr(value, name, property_value)


if not mesh_quality.native_audit_available():
    raise RuntimeError(
        "原生审计模块未加载：请确认 DatasmithMaterialAuditorEditor 已编译并重启 UE。"
    )
unreal.log("[NativeMeshAuditSmokeTest] stage=api_available")

if not hasattr(unreal, "DatasmithMeshAuditOptions"):
    raise RuntimeError("Missing unreal.DatasmithMeshAuditOptions")
if not hasattr(unreal, "DatasmithMeshAuditLibrary"):
    raise RuntimeError("Missing unreal.DatasmithMeshAuditLibrary")
analyze_static_mesh = getattr(
    unreal.DatasmithMeshAuditLibrary, "analyze_static_mesh", None
)
if not callable(analyze_static_mesh):
    raise RuntimeError("Missing DatasmithMeshAuditLibrary.analyze_static_mesh")

cube = unreal.EditorAssetLibrary.load_asset("/Engine/BasicShapes/Cube")
if not cube:
    raise RuntimeError("Engine cube asset is missing")
unreal.log("[NativeMeshAuditSmokeTest] stage=cube_loaded")

# First exercise the native API directly so a C++ binding/result regression is
# reported separately from selection or Python orchestration problems.
native_options = unreal.DatasmithMeshAuditOptions()
_set(native_options, "world_scale", unreal.Vector(1.0, 1.0, 1.0))
unreal.log("[NativeMeshAuditSmokeTest] stage=before_native_analyze")
native_result = analyze_static_mesh(cube, native_options)
unreal.log("[NativeMeshAuditSmokeTest] stage=after_native_analyze")
if not bool(_read(native_result, "success", False)):
    raise RuntimeError(
        "Native Engine Cube audit failed: {}".format(
            _read(native_result, "error", "unknown error")
        )
    )
triangle_count = int(_read(native_result, "triangle_count", 0))
uv_channel_count = int(_read(native_result, "uv_channel_count", 0))
if triangle_count <= 0:
    raise RuntimeError(
        "Native Engine Cube audit returned invalid triangle_count: {}".format(
            triangle_count
        )
    )
if uv_channel_count <= 0:
    raise RuntimeError(
        "Native Engine Cube audit returned invalid uv_channel_count: {}".format(
            uv_channel_count
        )
    )

# /Engine/BasicShapes/Cube is the closed-shell regression baseline. It must not
# produce any canonical 0.9.5 normal/topology issue (or a legacy equivalent).
forbidden_cube_issue_codes = {
    "INVALID_NORMAL",
    "DEGENERATE_TRIANGLE",
    "LOCAL_WINDING_CONFLICT",
    "VERTEX_NORMAL_REVERSED",
    "CLOSED_SHELL_INWARD",
    "NON_MANIFOLD_TOPOLOGY",
    "OPEN_MESH_DIRECTION_UNKNOWN",
    "ACTOR_NEGATIVE_SCALE",
    "INVALID_VERTEX_NORMAL",
    "NON_UNIT_VERTEX_NORMAL",
    "REVERSED_VERTEX_NORMAL",
    "AMBIGUOUS_VERTEX_NORMAL",
    "WINDING_CONFLICT",
    "NON_MANIFOLD_EDGE",
}
cube_issues = list(_read(native_result, "issues", []) or [])
cube_issue_codes = {
    str(_read(issue, "issue_code", "")).strip()
    for issue in cube_issues
}
unexpected_cube_issue_codes = sorted(
    code
    for code in cube_issue_codes
    if any(forbidden in code for forbidden in forbidden_cube_issue_codes)
)
if unexpected_cube_issue_codes:
    cube_issue_summary = [
        {
            "code": str(_read(issue, "issue_code", "")),
            "count": int(_read(issue, "count", 0)),
            "material_slot": int(_read(issue, "material_slot", -1)),
        }
        for issue in cube_issues
        if any(
            forbidden in str(_read(issue, "issue_code", "")).strip()
            for forbidden in forbidden_cube_issue_codes
        )
    ]
    raise RuntimeError(
        "Engine Cube produced false-positive normal/topology issues: {}".format(
            cube_issue_summary
        )
    )

group = mesh_quality.MeshAuditGroup(
    mesh_path=cube.get_path_name(),
    mesh_name=cube.get_name(),
    world_scale=(-1.0, 2.0, 1.0),
    uses=[
        mesh_quality.MeshComponentUse(
            actor_path="/Temp/DMA.NativeMeshAuditActor",
            actor_label="DMA_NativeMeshAuditActor",
            component_path="/Temp/DMA.NativeMeshAuditComponent",
            component_name="NativeMeshAuditComponent",
        )
    ],
    mesh=cube,
)

unreal.log("[NativeMeshAuditSmokeTest] stage=before_python_analyze")
rows = mesh_quality.analyze_mesh_groups([group])
unreal.log("[NativeMeshAuditSmokeTest] stage=after_python_analyze")
scale_rows = [
    row
    for row in rows
    if row.source == "instance_transform"
    and row.issue_code == "ACTOR_NEGATIVE_SCALE"
]
if not scale_rows:
    raise RuntimeError(
        "Negative scale audit warning was not generated: {}".format(
            [row.issue_code for row in rows]
        )
    )
if not all(row.world_scale[0] < 0.0 for row in scale_rows):
    raise RuntimeError(
        "Negative sign was lost in UI rows: {}".format(
            [row.world_scale for row in scale_rows]
        )
    )

unreal.log(
    "[NativeMeshAuditSmokeTest] PASS; triangles={}; uv_channels={}; "
    "groups={}; rows={}; module={}".format(
        triangle_count,
        uv_channel_count,
        1,
        len(rows),
        mesh_quality.__file__,
    )
)

# Release generated USTRUCT wrappers and UObject references while the native
# plugin and Python bridge are still fully alive. This keeps commandlet
# shutdown from deferring their destruction until module teardown.
del (
    rows,
    scale_rows,
    group,
    cube_issues,
    cube_issue_codes,
    unexpected_cube_issue_codes,
    native_result,
    native_options,
    cube,
    analyze_static_mesh,
)
import gc

gc.collect()
unreal.log("[NativeMeshAuditSmokeTest] stage=python_references_released")
