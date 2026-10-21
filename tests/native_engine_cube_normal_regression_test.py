# -*- coding: utf-8 -*-
"""UE 5.6 black-box regression for the 0.9.5 stock Engine Cube contract.

This is intentionally independent from native_mesh_audit_smoke_test.py. The
smoke test checks availability and orchestration; this test locks down closed-
shell orientation and prevents vertex-normal/topology false positives.
It only reads /Engine/BasicShapes/Cube and never creates or saves an asset.
"""

import unreal


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


def _issue_code(issue):
    return str(_read(issue, "issue_code", "")).strip().upper()


if not hasattr(unreal, "DatasmithMeshAuditLibrary"):
    raise RuntimeError("DatasmithMaterialAuditor native audit module is not loaded")

analyze_static_mesh = getattr(
    unreal.DatasmithMeshAuditLibrary, "analyze_static_mesh", None
)
if not callable(analyze_static_mesh):
    raise RuntimeError("Missing DatasmithMeshAuditLibrary.analyze_static_mesh")

cube = unreal.EditorAssetLibrary.load_asset("/Engine/BasicShapes/Cube")
if not cube:
    raise RuntimeError("Missing /Engine/BasicShapes/Cube")

options = unreal.DatasmithMeshAuditOptions()
_set(options, "world_scale", unreal.Vector(1.0, 1.0, 1.0))
result = analyze_static_mesh(cube, options)
if not bool(_read(result, "success", False)):
    raise RuntimeError(
        "Engine Cube audit failed: {}".format(_read(result, "error", "unknown"))
    )

forbidden_codes = {
    "INVALID_NORMAL",
    "DEGENERATE_TRIANGLE",
    "LOCAL_WINDING_CONFLICT",
    "VERTEX_NORMAL_REVERSED",
    "CLOSED_SHELL_INWARD",
    "NON_MANIFOLD_TOPOLOGY",
    "OPEN_MESH_DIRECTION_UNKNOWN",
    "ACTOR_NEGATIVE_SCALE",
    "REVERSED_VERTEX_NORMAL",
    "AMBIGUOUS_VERTEX_NORMAL",
    "INVALID_VERTEX_NORMAL",
    "NON_UNIT_VERTEX_NORMAL",
    "WINDING_CONFLICT",
    "NON_MANIFOLD_EDGE",
    "DEGENERATE_GEOMETRY",
}
issues = list(_read(result, "issues", []) or [])
normal_data_source = str(_read(result, "normal_data_source", "") or "")
if normal_data_source != "source_mesh_description_lod0":
    raise RuntimeError(
        "Engine Cube returned unexpected source ID space: {!r}".format(
            normal_data_source
        )
    )
bad = []
for issue in issues:
    code = _issue_code(issue)
    # UE's FName wrapper can stringify as either NAME or <Name NAME> depending
    # on the Python bridge build. Substring matching keeps this test focused on
    # the stable language-neutral issue code rather than wrapper formatting.
    if any(forbidden in code for forbidden in forbidden_codes):
        bad.append(
            "{}(count={})".format(code, int(_read(issue, "count", 0)))
        )

if bad:
    raise RuntimeError(
        "Stock Engine Cube produced geometry/normal false positives: {}".format(
            ", ".join(bad)
        )
    )

unreal.log(
    "[NativeEngineCubeNormalRegression] PASS; triangles={}; issues={}".format(
        int(_read(result, "triangle_count", 0)), len(issues)
    )
)

# Release USTRUCT/UObject wrappers before commandlet module teardown.
del issues, bad, result, options, cube, analyze_static_mesh
import gc

gc.collect()
