# -*- coding: utf-8 -*-
"""Pure-Python regression for mesh-quality semantics and native option transfer."""

from __future__ import annotations

import os
import sys
import types


class FakeNativeAuditOptions:
    def __init__(self):
        self.assigned = {}

    def set_editor_property(self, name, value):
        self.assigned[name] = value
        setattr(self, name, value)


class FakeVector:
    def __init__(self, x, y, z):
        self.x = x
        self.y = y
        self.z = z


fake_unreal = types.SimpleNamespace(
    StaticMeshComponent=type("StaticMeshComponent", (), {}),
    DatasmithMeshAuditOptions=FakeNativeAuditOptions,
    DatasmithMeshAuditLibrary=types.SimpleNamespace(
        analyze_static_mesh=lambda _mesh, _options: None,
        analyze_scene_component_transform=lambda component: component.transform_result,
    ),
    Vector=FakeVector,
    log=lambda _message: None,
    log_warning=lambda _message: None,
)
sys.modules["unreal"] = fake_unreal

PLUGIN_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PYTHON_ROOT = os.path.join(PLUGIN_ROOT, "Content", "Python")
if PYTHON_ROOT not in sys.path:
    sys.path.insert(0, PYTHON_ROOT)

from material_auditor import mesh_quality


expected_units = {
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
for issue_code, unit in expected_units.items():
    assert mesh_quality.affected_element_unit(issue_code) == unit, issue_code
    assert mesh_quality.format_affected_elements(issue_code, 12) == "12 {}".format(
        unit
    ), issue_code

for issue_code, category in (
    ("ACTOR_NEGATIVE_SCALE", "transform"),
    ("NATIVE_ANALYSIS_FAILED", "system"),
):
    assert mesh_quality.affected_element_unit(issue_code, category) == "", issue_code
    assert mesh_quality.format_affected_elements(issue_code, 0, category) == "—"

assert mesh_quality.affected_element_unit("FUTURE_NATIVE_ISSUE", "geometry") == "项"
assert mesh_quality.format_affected_elements("FUTURE_NATIVE_ISSUE", 3) == "3 项"
assert mesh_quality._issue_category("NON_MANIFOLD_EDGE", "") == "geometry"
assert mesh_quality._issue_category("NON_MANIFOLD_TOPOLOGY", "") == "geometry"
assert mesh_quality._issue_category("WINDING_CONFLICT", "") == "geometry"
assert mesh_quality._issue_category("LOCAL_WINDING_CONFLICT", "") == "geometry"
assert mesh_quality._issue_category("INVALID_VERTEX_NORMAL", "") == "normal"
assert mesh_quality._issue_category("INVALID_NORMAL", "") == "normal"
assert mesh_quality._issue_category("VERTEX_NORMAL_REVERSED", "") == "normal"
assert mesh_quality._issue_category("CLOSED_SHELL_INWARD", "") == "normal"
assert mesh_quality._issue_category("OPEN_MESH_DIRECTION_UNKNOWN", "") == "normal"
assert mesh_quality._issue_category("ACTOR_NEGATIVE_SCALE", "") == "transform"
assert mesh_quality._issue_category("UV_STRETCH", "") == "uv"

# The standard 0.9.5 audit runs the conservative topology/normal pass. Lenient
# remains an explicit opt-out while strict keeps it enabled with tighter data
# validation thresholds.
lenient = mesh_quality.options_for_preset("lenient")
standard = mesh_quality.options_for_preset("standard")
strict = mesh_quality.options_for_preset("strict")
assert lenient.report_normal_direction_mismatch is False
assert standard.report_normal_direction_mismatch is True
assert strict.report_normal_direction_mismatch is True
for preset in (lenient, standard, strict):
    assert preset.normal_dot_error_threshold == -0.2
    assert preset.normal_dot_warning_threshold == 0.2
    assert preset.report_ambiguous_normals is True
assert standard.report_non_unit_normals is True
assert strict.report_non_unit_normals is True

# Verify that the stable Python setting is forwarded under the exact generated
# UE Python property name expected by FDatasmithMeshAuditOptions.
native_standard = mesh_quality.build_native_options(standard, (1.0, 2.0, 3.0))
native_strict = mesh_quality.build_native_options(strict, (4.0, 5.0, 6.0))
assert native_standard.assigned["report_normal_direction_mismatch"] is True
assert native_strict.assigned["report_normal_direction_mismatch"] is True
assert native_standard.assigned["normal_dot_error_threshold"] == -0.2
assert native_standard.assigned["normal_dot_warning_threshold"] == 0.2
assert native_standard.assigned["report_non_unit_normals"] is True
assert native_strict.assigned["report_non_unit_normals"] is True
assert (
    native_strict.world_scale.x,
    native_strict.world_scale.y,
    native_strict.world_scale.z,
) == (4.0, 5.0, 6.0)

# Instance mirroring is a Python/Actor-level warning. It must not be folded
# into any source-mesh normal category or presented as a native finding.
mirrored_group = mesh_quality.MeshAuditGroup(
    mesh_path="/Game/Test/SM_Healthy",
    mesh_name="SM_Healthy",
    world_scale=(-1.0, 2.0, 1.0),
    uses=[
        mesh_quality.MeshComponentUse(
            actor_path="/Temp/TestActor",
            actor_label="TestActor",
            component_path="/Temp/TestActor.Mesh",
            component_name="Mesh",
            transform_determinant=-2.0,
            orientation_reversing=True,
        )
    ],
    mesh=object(),
)
scale_rows = mesh_quality._scale_issue_rows(mirrored_group, standard)
negative_scale_rows = [
    row for row in scale_rows if row.issue_code == "ACTOR_NEGATIVE_SCALE"
]
assert len(negative_scale_rows) == 1
assert negative_scale_rows[0].severity == "warning"
assert negative_scale_rows[0].category == "transform"
assert negative_scale_rows[0].source == "instance_transform"
assert negative_scale_rows[0].details["orientation_reversing"] is True
assert negative_scale_rows[0].details["transform_determinant"] == -2.0
assert negative_scale_rows[0].actor_paths == ["/Temp/TestActor"]
assert negative_scale_rows[0].component_paths == ["/Temp/TestActor.Mesh"]
assert negative_scale_rows[0].component_count == 1
assert not [row for row in scale_rows if row.category == "normal"]

# Scale signs are not the authority.  A positive full-matrix determinant must
# not be reported even when the compatibility world-scale tuple has one
# negative axis, while a negative determinant must be reported for a positive
# displayed tuple.
positive_determinant_group = mesh_quality.MeshAuditGroup(
    mesh_path="/Game/Test/SM_PositiveDeterminant",
    mesh_name="SM_PositiveDeterminant",
    world_scale=(-1.0, 1.0, 1.0),
    uses=[
        mesh_quality.MeshComponentUse(
            actor_path="/Temp/PositiveActor",
            actor_label="PositiveActor",
            component_path="/Temp/PositiveActor.Mesh",
            component_name="Mesh",
            transform_determinant=3.0,
            orientation_reversing=False,
        )
    ],
    mesh=object(),
)
assert not [
    row
    for row in mesh_quality._scale_issue_rows(positive_determinant_group, standard)
    if row.issue_code == "ACTOR_NEGATIVE_SCALE"
]

negative_determinant_group = mesh_quality.MeshAuditGroup(
    mesh_path="/Game/Test/SM_NegativeDeterminant",
    mesh_name="SM_NegativeDeterminant",
    world_scale=(1.0, 1.0, 1.0),
    uses=[
        mesh_quality.MeshComponentUse(
            actor_path="/Temp/NegativeActor",
            actor_label="NegativeActor",
            component_path="/Temp/NegativeActor.Mesh",
            component_name="Mesh",
            transform_determinant=-0.5,
            orientation_reversing=True,
        )
    ],
    mesh=object(),
)
matrix_rows = mesh_quality._scale_issue_rows(negative_determinant_group, standard)
assert [row.issue_code for row in matrix_rows] == ["ACTOR_NEGATIVE_SCALE"]


class FakeComponent:
    def __init__(self, determinant, negative):
        self.transform_result = {
            "success": True,
            "determinant": determinant,
            "negative_determinant": negative,
        }

    def get_path_name(self):
        return "/Temp/FakeActor.Mesh"


assert mesh_quality._analyze_scene_component_transform(
    FakeComponent(-4.0, True)
) == (-4.0, True)
assert mesh_quality._analyze_scene_component_transform(
    FakeComponent(4.0, False)
) == (4.0, False)

try:
    mesh_quality._analyze_scene_component_transform(FakeComponent(-4.0, False))
except RuntimeError as exc:
    assert "自相矛盾" in str(exc)
else:
    raise AssertionError("Contradictory native determinant result was accepted")

# A stale native DLL must fail explicitly rather than silently using XYZ scale
# multiplication as a fallback orientation test.
native_library = fake_unreal.DatasmithMeshAuditLibrary
saved_transform_api = native_library.analyze_scene_component_transform
del native_library.analyze_scene_component_transform
try:
    mesh_quality._native_api()
except mesh_quality.NativeAuditUnavailableError as exc:
    assert "analyze_scene_component_transform" in str(exc)
    assert "不会退回" in str(exc)
else:
    raise AssertionError("Stale DLL without transform API was accepted")
finally:
    native_library.analyze_scene_component_transform = saved_transform_api

print("pure_mesh_quality_semantics_test: PASS")
