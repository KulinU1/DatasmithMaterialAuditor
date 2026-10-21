# -*- coding: utf-8 -*-
"""Pure-Python regression for selection, descendant, signed-scale grouping."""

from __future__ import annotations

import os
import sys
import types


class _StaticMeshComponent:
    pass


class _InstancedStaticMeshComponent(_StaticMeshComponent):
    pass


warnings = []


def analyze_scene_component_transform(component):
    return types.SimpleNamespace(
        success=True,
        error="",
        determinant=component.determinant,
        orientation_reversing=component.determinant < 0.0,
    )


fake_unreal = types.SimpleNamespace(
    StaticMeshComponent=_StaticMeshComponent,
    InstancedStaticMeshComponent=_InstancedStaticMeshComponent,
    DatasmithMeshAuditOptions=type("DatasmithMeshAuditOptions", (), {}),
    DatasmithMeshAuditLibrary=types.SimpleNamespace(
        analyze_static_mesh=lambda _mesh, _options: None,
        analyze_scene_component_transform=analyze_scene_component_transform,
    ),
    log=lambda _message: None,
    log_warning=warnings.append,
)
sys.modules["unreal"] = fake_unreal

PLUGIN_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PYTHON_ROOT = os.path.join(PLUGIN_ROOT, "Content", "Python")
if PYTHON_ROOT not in sys.path:
    sys.path.insert(0, PYTHON_ROOT)

from material_auditor import mesh_quality


class FakeMesh:
    def get_path_name(self):
        return "/Game/Test/SM_Shared.SM_Shared"

    def get_name(self):
        return "SM_Shared"


class FakeComponent:
    def __init__(self, path, mesh, scale, determinant):
        self.path = path
        self.mesh = mesh
        self.scale = scale
        self.determinant = determinant

    def get_path_name(self):
        return self.path

    def get_name(self):
        return self.path.rsplit(".", 1)[-1]

    def get_editor_property(self, name):
        if name == "static_mesh":
            return self.mesh
        raise AttributeError(name)

    def get_world_scale(self):
        return self.scale


class FakeInstancedComponent(FakeComponent, _InstancedStaticMeshComponent):
    pass


class FakeActor:
    def __init__(self, path, label, parent, components):
        self.path = path
        self.label = label
        self.parent = parent
        self.components = components

    def get_path_name(self):
        return self.path

    def get_actor_label(self):
        return self.label

    def get_attach_parent_actor(self):
        return self.parent

    def get_components_by_class(self, _component_class):
        return list(self.components)


mesh = FakeMesh()
root = FakeActor(
    "/Game/TestLevel.TestLevel:PersistentLevel.Root",
    "Root",
    None,
    [FakeComponent("/Temp/Root.Mesh", mesh, (-1.0, 2.0, 1.0), -2.0)],
)
child = FakeActor(
    "/Game/TestLevel.TestLevel:PersistentLevel.Child",
    "Child",
    root,
    # Same decomposed scale as Root, but a positive native matrix determinant.
    [FakeComponent("/Temp/Child.Mesh", mesh, (-1.0, 2.0, 1.0), 2.0)],
)
other_scale = FakeActor(
    "/Game/TestLevel.TestLevel:PersistentLevel.OtherScale",
    "OtherScale",
    root,
    # Positive-looking generated scale, but the complete native matrix reverses orientation.
    [FakeComponent("/Temp/OtherScale.Mesh", mesh, (1.0, 2.0, 1.0), -2.0)],
)
instanced = FakeActor(
    "/Game/TestLevel.TestLevel:PersistentLevel.Instanced",
    "Instanced",
    root,
    [FakeInstancedComponent("/Temp/Instanced.Mesh", mesh, (1.0, 1.0, 1.0), 1.0)],
)

original_selected = mesh_quality._selected_level_actors
original_all = mesh_quality._all_level_actors
try:
    mesh_quality._selected_level_actors = lambda: [root]
    mesh_quality._all_level_actors = lambda: [root, child, other_scale, instanced]
    groups = mesh_quality.collect_selected_mesh_groups()
finally:
    mesh_quality._selected_level_actors = original_selected
    mesh_quality._all_level_actors = original_all

assert len(groups) == 2, [(item.mesh_path, item.world_scale) for item in groups]
negative = [item for item in groups if item.world_scale[0] < 0.0]
positive = [item for item in groups if item.world_scale[0] > 0.0]
assert len(negative) == 1 and negative[0].component_count == 2, negative
assert len(positive) == 1 and positive[0].component_count == 1, positive
assert negative[0].world_scale == (-1.0, 2.0, 1.0), negative[0].world_scale
assert {use.actor_label for use in negative[0].uses} == {"Root", "Child"}
assert {
    (use.actor_label, use.transform_determinant, use.orientation_reversing)
    for use in negative[0].uses
} == {("Root", -2.0, True), ("Child", 2.0, False)}
assert {
    (use.actor_label, use.transform_determinant, use.orientation_reversing)
    for use in positive[0].uses
} == {("OtherScale", -2.0, True)}

transform_rows = []
for group in groups:
    transform_rows.extend(mesh_quality._scale_issue_rows(group, mesh_quality.MeshQualityOptions()))
negative_determinant_rows = [
    row for row in transform_rows if row.issue_code == "ACTOR_NEGATIVE_SCALE"
]
assert len(negative_determinant_rows) == 2, negative_determinant_rows
assert all(row.component_count == 1 for row in negative_determinant_rows)
assert {row.actor_label for row in negative_determinant_rows} == {"Root", "OtherScale"}
assert {
    row.details["transform_determinant"] for row in negative_determinant_rows
} == {-2.0}
assert all(
    "Instanced" not in {use.actor_label for use in group.uses} for group in groups
), groups
assert any("ISM/HISM" in message for message in warnings), warnings

print("pure_mesh_quality_grouping_test: PASS")
