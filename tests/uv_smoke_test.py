# -*- coding: utf-8 -*-
"""UE 5.6 Geometry Script smoke test without saving project assets."""

import os
import sys

import unreal


PLUGIN_PYTHON = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "Content", "Python")
)
if PLUGIN_PYTHON not in sys.path:
    sys.path.insert(0, PLUGIN_PYTHON)

for module_name in list(sys.modules):
    if module_name == "material_auditor" or module_name.startswith(
        "material_auditor."
    ):
        del sys.modules[module_name]

from material_auditor import core, version


if core.API_VERSION != version.CORE_API_VERSION:
    raise RuntimeError("Loaded wrong plugin source: {}".format(core.__file__))

cube = unreal.EditorAssetLibrary.load_asset("/Engine/BasicShapes/Cube")
if not cube:
    raise RuntimeError("Engine cube asset is missing")

# Exercise the same read -> Box Projection -> write pipeline on a transient
# Static Mesh. /Engine/Transient is explicitly accepted by Geometry Script and
# disappears with this commandlet process, so the project is not dirtied.
transient_target = unreal.StaticMesh()
dimensions = core._write_box_projection_to_static_mesh(
    cube,
    transient_target,
    (2.0, 4.0, 8.0),
    200.0,
    0,
)
expected = (100.0, 50.0, 25.0)
if any(abs(dimensions[index] - expected[index]) > 0.001 for index in range(3)):
    raise RuntimeError(
        "Scale compensation mismatch: {} != {}".format(dimensions, expected)
    )

# Exercise direct current-selection scanning and automatic classification.
actor_subsystem = unreal.get_editor_subsystem(unreal.EditorActorSubsystem)
actor = actor_subsystem.spawn_actor_from_class(
    unreal.StaticMeshActor, unreal.Vector(0.0, 0.0, 0.0)
)
original_selected = core.selected_level_actors
original_all = core.all_level_actors
try:
    actor.static_mesh_component.set_static_mesh(cube)
    core.selected_level_actors = lambda: [actor]
    core.all_level_actors = lambda: [actor]
    records = core.scan_uv_assets(
        200.0,
        50.0,
        4.0,
        root_paths=None,
    )
    if len(records) != 1:
        raise RuntimeError("Expected one UV record, got {}".format(len(records)))
    record = records[0]
    if record.classification != "small" or record.projection_size != 50.0:
        raise RuntimeError("Unexpected cube classification: {}".format(record))
finally:
    core.selected_level_actors = original_selected
    core.all_level_actors = original_all
    actor_subsystem.destroy_actor(actor)

unreal.log(
    "[MaterialAuditorUVSmokeTest] PASS; core={}; dimensions={}".format(
        core.__file__, dimensions
    )
)
