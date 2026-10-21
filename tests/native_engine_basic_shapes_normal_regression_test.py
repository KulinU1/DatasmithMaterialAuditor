# -*- coding: utf-8 -*-
"""UE 5.6 black-box 0.9.5 regression for stock Engine basic shapes.

Cube, Plane, and Sphere provide hard-edge, planar, and smooth-normal baselines.
The script is read-only: it neither creates nor saves assets.
"""

import gc

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

forbidden_codes = {
    "INVALID_NORMAL",
    "DEGENERATE_TRIANGLE",
    "LOCAL_WINDING_CONFLICT",
    "VERTEX_NORMAL_REVERSED",
    "CLOSED_SHELL_INWARD",
    "NON_MANIFOLD_TOPOLOGY",
    "ACTOR_NEGATIVE_SCALE",
    "REVERSED_VERTEX_NORMAL",
    "AMBIGUOUS_VERTEX_NORMAL",
    "INVALID_VERTEX_NORMAL",
    "NON_UNIT_VERTEX_NORMAL",
    "WINDING_CONFLICT",
    "NON_MANIFOLD_EDGE",
    "DEGENERATE_GEOMETRY",
}

shape_paths = (
    "/Engine/BasicShapes/Cube",
    "/Engine/BasicShapes/Plane",
    "/Engine/BasicShapes/Sphere",
)
summaries = []
for shape_path in shape_paths:
    mesh = unreal.EditorAssetLibrary.load_asset(shape_path)
    if not mesh:
        raise RuntimeError("Missing stock Engine shape: {}".format(shape_path))

    scale_cases = (
        ("identity", unreal.Vector(1.0, 1.0, 1.0)),
        ("mirrored-instance", unreal.Vector(-1.0, 1.0, 1.0)),
        ("non-uniform-instance", unreal.Vector(2.0, 0.5, 3.0)),
    )
    shape_code_baseline = None
    for scale_name, world_scale in scale_cases:
        options = unreal.DatasmithMeshAuditOptions()
        _set(options, "world_scale", world_scale)
        _set(options, "report_ambiguous_normals", False)
        _set(options, "report_non_unit_normals", False)
        result = analyze_static_mesh(mesh, options)
        if not bool(_read(result, "success", False)):
            raise RuntimeError(
                "{} ({}) audit failed: {}".format(
                    shape_path,
                    scale_name,
                    _read(result, "error", "unknown"),
                )
            )

        normal_data_source = str(_read(result, "normal_data_source", "") or "")
        if normal_data_source != "source_mesh_description_lod0":
            raise RuntimeError(
                "{} ({}) returned unknown normal data source: {!r}".format(
                    shape_path, scale_name, normal_data_source
                )
            )

        issues = list(_read(result, "issues", []) or [])
        issue_codes = {_issue_code(issue) for issue in issues}
        bad = []
        for issue in issues:
            code = _issue_code(issue)
            if any(forbidden in code for forbidden in forbidden_codes):
                bad.append(
                    "{}(count={})".format(code, int(_read(issue, "count", 0)))
                )

        if bad:
            raise RuntimeError(
                "{} ({}) produced geometry/normal false positives: {}".format(
                    shape_path, scale_name, ", ".join(bad)
                )
            )

        open_direction_issues = [
            issue
            for issue in issues
            if "OPEN_MESH_DIRECTION_UNKNOWN" in _issue_code(issue)
        ]
        if any(
            "INFO" not in str(_read(issue, "severity", "")).upper()
            for issue in open_direction_issues
        ):
            raise RuntimeError(
                "{} ({}) promoted OPEN_MESH_DIRECTION_UNKNOWN above Info".format(
                    shape_path, scale_name
                )
            )
        if shape_path.endswith("/Plane") and not open_direction_issues:
            raise RuntimeError(
                "Engine Plane ({}) did not report its informational open-mesh state".format(
                    scale_name
                )
            )
        if shape_path.endswith("/Cube") and open_direction_issues:
            raise RuntimeError("Engine Cube was incorrectly classified as open")

        if shape_code_baseline is None:
            shape_code_baseline = issue_codes
        elif issue_codes != shape_code_baseline:
            raise RuntimeError(
                "{} native issue codes changed under Actor scale {}: {} != {}".format(
                    shape_path,
                    scale_name,
                    sorted(issue_codes),
                    sorted(shape_code_baseline),
                )
            )

        summaries.append(
            "{}[{}]:triangles={},issues={},normals={}".format(
                shape_path.rsplit("/", 1)[-1],
                scale_name,
                int(_read(result, "triangle_count", 0)),
                len(issues),
                normal_data_source,
            )
        )
        del issues, issue_codes, open_direction_issues, bad, result, options
    del mesh

unreal.log("[NativeEngineBasicShapesNormalRegression] PASS; " + "; ".join(summaries))

del summaries, analyze_static_mesh
gc.collect()
