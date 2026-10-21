# -*- coding: utf-8 -*-
"""Regression test for the Actor-grouped mesh-quality result tree."""

import unreal

from material_auditor import mesh_quality, ui


def make_row(
    severity,
    actor_paths,
    issue_code,
    mesh_name,
    category="normal",
    material_slot=0,
    triangle_ids=None,
):
    first_path = actor_paths[0]
    component_paths = [path + ".StaticMeshComponent0" for path in actor_paths]
    return mesh_quality.MeshQualityRow(
        severity=severity,
        category=category,
        issue_code=issue_code,
        issue_type=issue_code,
        message="UI grouping test",
        mesh_path="/Engine/BasicShapes/{}.{}".format(mesh_name, mesh_name),
        mesh_name=mesh_name,
        world_scale=(1.0, 1.0, 1.0),
        actor_path=first_path,
        actor_label=first_path.rsplit(".", 1)[-1],
        component_path=component_paths[0],
        component_name="StaticMeshComponent0",
        actor_paths=list(actor_paths),
        actor_labels=[path.rsplit(".", 1)[-1] for path in actor_paths],
        component_paths=component_paths,
        component_names=["StaticMeshComponent0"] * len(component_paths),
        component_count=len(component_paths),
        material_slot=material_slot,
        affected_count=3,
        max_metric=1.0,
        lod_index=0,
        region_id="test-region-{}".format(issue_code.lower()),
        region_index=0,
        error_triangle_ids=list(triangle_ids or []),
        triangle_id_space="source_mesh_description_lod0",
        triangle_ids=list(triangle_ids or []),
    )


actor_a = "/Game/Test.Test:PersistentLevel.Actor_A"
actor_b = "/Game/Test.Test:PersistentLevel.Actor_B"
window = ui.show_window()
window.mesh_quality_rows = [
    make_row(
        "warning",
        [actor_a, actor_b],
        "VERTEX_NORMAL_REVERSED",
        "Cube",
        triangle_ids=[3],
    ),
    make_row(
        "error",
        [actor_b],
        "DEGENERATE_TRIANGLE",
        "Plane",
        category="geometry",
        material_slot=3,
        triangle_ids=[7, 9],
    ),
]
window._populate_mesh_quality_table()

tree = window.mesh_quality_table
if tree.topLevelItemCount() != 2:
    raise RuntimeError("Expected two Actor groups")
group_a = tree.topLevelItem(0)
group_b = tree.topLevelItem(1)
if group_a.text(0) != "Actor_A" or group_b.text(0) != "Actor_B":
    raise RuntimeError("Actor groups are not sorted by label")
if group_a.childCount() != 1 or group_b.childCount() != 2:
    raise RuntimeError("Issue rows were not duplicated into their owning Actor groups")
if group_a.isExpanded() or group_b.isExpanded():
    raise RuntimeError("Actor groups must start collapsed")
if "法线 1" not in group_a.text(9) or "几何 0" not in group_a.text(9):
    raise RuntimeError("Actor A category summary is incomplete")
if "法线 1" not in group_b.text(9) or "几何 1" not in group_b.text(9):
    raise RuntimeError("Actor B category summary is incomplete")

geometry_issue = group_b.child(0)
if "材质槽 3" not in geometry_issue.text(9):
    raise RuntimeError("Issue description does not show the material slot")
if "Triangle IDs 7, 9" not in geometry_issue.text(9):
    raise RuntimeError("Issue description does not show Triangle IDs")
if "材质槽：3" not in geometry_issue.toolTip(9):
    raise RuntimeError("Issue tooltip does not show the material slot")
if "Triangle IDs：7, 9" not in geometry_issue.toolTip(9):
    raise RuntimeError("Issue tooltip does not show Triangle IDs")

tree.clearSelection()
group_b.setSelected(True)
selected = window._selected_mesh_quality_rows()
if len(selected) != 2 or any(row.actor_paths != [actor_b] for row in selected):
    raise RuntimeError("Selecting an Actor group must target only that Actor's issues")

error_index = window.mesh_quality_severity_filter.findData("error")
window.mesh_quality_severity_filter.setCurrentIndex(error_index)
if not group_a.isHidden() or group_b.isHidden():
    raise RuntimeError("Actor group visibility did not follow the issue filter")
if group_b.child(0).isHidden() or not group_b.child(1).isHidden():
    raise RuntimeError("Issue rows did not follow the severity filter")
if group_b.text(1) != "显示 1 / 2 个问题":
    raise RuntimeError("Actor group does not summarize filtered issue counts")
if "法线 0" not in group_b.text(9) or "几何 1" not in group_b.text(9):
    raise RuntimeError("Actor category counts do not follow the active filter")


class FakeTransform:
    def __init__(self, offset):
        self.offset = offset

    def transform_location(self, point):
        return unreal.Vector(
            point.x + self.offset[0],
            point.y + self.offset[1],
            point.z + self.offset[2],
        )


class WorldTransformOnlyComponent:
    """UE 5.6-style component with no get_component_transform method."""

    def get_world_transform(self):
        return FakeTransform((10.0, 20.0, 30.0))


class PropertyTransformOnlyComponent:
    def get_editor_property(self, name):
        if name == "component_to_world":
            return FakeTransform((20.0, 30.0, 40.0))
        raise AttributeError(name)


class SocketTransformOnlyComponent:
    def get_socket_transform(self, *_args):
        return FakeTransform((30.0, 40.0, 50.0))


class IdentityRotation:
    def rotate_vector(self, value):
        return value


class WorldTRSOnlyComponent:
    def get_world_location(self):
        return unreal.Vector(40.0, 50.0, 60.0)

    def get_world_rotation(self):
        return IdentityRotation()

    def get_world_scale(self):
        return unreal.Vector(2.0, 3.0, 4.0)


def assert_point(component, expected):
    result = window._transform_mesh_quality_point(component, (1.0, 2.0, 3.0))
    actual = (float(result.x), float(result.y), float(result.z))
    if any(abs(actual[index] - expected[index]) > 1.0e-5 for index in range(3)):
        raise RuntimeError("Unexpected transformed point: {} != {}".format(actual, expected))


assert_point(WorldTransformOnlyComponent(), (11.0, 22.0, 33.0))
assert_point(PropertyTransformOnlyComponent(), (21.0, 32.0, 43.0))
assert_point(SocketTransformOnlyComponent(), (31.0, 42.0, 53.0))
assert_point(WorldTRSOnlyComponent(), (42.0, 56.0, 72.0))

# Exercise the actual UE 5.6 StaticMeshComponent binding as well as the API
# compatibility fakes above.  The actor is transient test-project state and is
# always destroyed without saving the level.
actor_subsystem = unreal.get_editor_subsystem(unreal.EditorActorSubsystem)
test_mesh = unreal.EditorAssetLibrary.load_asset("/Engine/BasicShapes/Cube")
if not test_mesh:
    raise RuntimeError("Unable to load Engine Cube for transform regression")
test_actor = actor_subsystem.spawn_actor_from_object(
    test_mesh,
    unreal.Vector(100.0, 200.0, 300.0),
    unreal.Rotator(0.0, 0.0, 0.0),
)
if not test_actor:
    raise RuntimeError("Unable to spawn transform regression actor")
try:
    test_actor.set_actor_scale3d(unreal.Vector(2.0, 3.0, 4.0))
    real_component = test_actor.get_editor_property("static_mesh_component")
    assert_point(real_component, (102.0, 206.0, 312.0))

    real_actor_path = str(test_actor.get_path_name())
    real_component_path = str(real_component.get_path_name())
    highlight_row = make_row(
        "error",
        [real_actor_path],
        "DEGENERATE_TRIANGLE",
        "Cube",
        category="geometry",
        triangle_ids=[0],
    )
    highlight_row.component_path = real_component_path
    highlight_row.component_paths = [real_component_path]
    highlight_row.component_name = str(real_component.get_name())
    highlight_row.component_names = [highlight_row.component_name]
    highlight_row.sample_vertices = [
        (50.0, -50.0, -50.0),
        (50.0, 50.0, -50.0),
        (50.0, 50.0, 50.0),
    ]
    window.mesh_quality_rows = [highlight_row]
    window._populate_mesh_quality_table()
    highlight_item = window.mesh_quality_table.topLevelItem(0).child(0)
    highlight_item.setSelected(True)
    window.preview_next_mesh_quality_sample()
    preview_state = window._mesh_quality_preview_state
    if not preview_state or len(preview_state.get("entries", [])) != 1:
        raise RuntimeError("Solid face highlight did not retain one preview entry")
    if window._draw_mesh_quality_preview() != 1:
        raise RuntimeError("Solid face highlight did not draw the selected triangle")
    window.clear_mesh_quality_preview(log_message=False)
finally:
    actor_subsystem.destroy_actor(test_actor)

window.close()
unreal.log("[MeshQualityActorTreeUITest] PASS")
