# -*- coding: utf-8 -*-
"""Read-only UE commandlet smoke test."""

import json
import os
import sys

import unreal

PLUGIN_PYTHON = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "Content", "Python")
)
if PLUGIN_PYTHON not in sys.path:
    sys.path.insert(0, PLUGIN_PYTHON)

import material_auditor
from material_auditor import core, version

if core.API_VERSION != version.CORE_API_VERSION:
    raise RuntimeError(
        "Expected installed plugin {}, loaded {} from {}".format(
            version.CORE_API_VERSION, core.API_VERSION, core.__file__
        )
    )


rules_file = os.path.join(
    os.path.dirname(core.__file__),
    "default_rules.json",
)
rules = core.load_rules(rules_file)
required_keys = {
    "glass_keywords",
    "exclude_keywords",
    "glass_material_paths",
    "non_glass_material_paths",
    "excluded_material_paths",
}
missing = sorted(required_keys.difference(rules))
if missing:
    raise RuntimeError("Missing rule keys: {}".format(json.dumps(missing)))

assert hasattr(unreal, "EditorActorSubsystem")
assert hasattr(unreal, "UnrealEditorSubsystem")
assert hasattr(unreal, "MaterialEditingLibrary")
assert hasattr(unreal, "MaterialExpressionMaterialFunctionCall")
assert hasattr(unreal, "AutomationLibrary")
assert hasattr(unreal.AutomationLibrary, "set_editor_viewport_visualize_buffer")
assert hasattr(unreal, "MaterialInstanceBasePropertyOverrides")

overrides = unreal.MaterialInstanceBasePropertyOverrides()
overrides.set_editor_property("override_blend_mode", True)
overrides.set_editor_property("blend_mode", unreal.BlendMode.BLEND_OPAQUE)
overrides.set_editor_property("override_shading_model", True)
overrides.set_editor_property(
    "shading_model", unreal.MaterialShadingModel.MSM_DEFAULT_LIT
)
assert overrides.get_editor_property("override_blend_mode")
assert overrides.get_editor_property("override_shading_model")

transient_base = unreal.Material()
assert not core.material_normal_connected(transient_base)
transient_base.set_editor_property("two_sided", False)
assert not core.material_two_sided(transient_base)
transient_base.set_editor_property("two_sided", True)
assert core.material_two_sided(transient_base)
transient_base.set_editor_property(
    "blend_mode", unreal.BlendMode.BLEND_TRANSLUCENT
)
blend_token = core._blend_mode_token(
    transient_base.get_editor_property("blend_mode")
)
assert blend_token == "BLEND_TRANSLUCENT", blend_token
assert core._blend_mode_from_token(blend_token) == unreal.BlendMode.BLEND_TRANSLUCENT

if unreal.EditorAssetLibrary.does_asset_exist(
    core.package_path(core.DEFAULT_POST_DEPTH_MATERIAL)
):
    assert core.load_material(core.DEFAULT_POST_DEPTH_MATERIAL)
test_function = core.load_material_function(
    "/Engine/Functions/Engine_MaterialFunctions02/SafeNormalize"
)
assert test_function
function_call = unreal.MaterialEditingLibrary.create_material_expression(
    transient_base,
    unreal.MaterialExpressionMaterialFunctionCall,
    -400,
    0,
)
assert function_call
assert function_call.set_material_function(test_function)
assert unreal.MaterialEditingLibrary.connect_material_property(
    function_call,
    "",
    unreal.MaterialProperty.MP_NORMAL,
)
assert core.material_normal_connected(transient_base)

# Build a transient editor actor so component/material scan APIs are exercised
# without touching or saving project content.
actor_subsystem = unreal.get_editor_subsystem(unreal.EditorActorSubsystem)
actor = actor_subsystem.spawn_actor_from_class(
    unreal.StaticMeshActor, unreal.Vector(0.0, 0.0, 0.0)
)
cube = unreal.EditorAssetLibrary.load_asset("/Engine/BasicShapes/Cube")
actor.static_mesh_component.set_static_mesh(cube)
original_selected_level_actors = core.selected_level_actors
original_all_level_actors = core.all_level_actors
core.selected_level_actors = lambda: [actor]
core.all_level_actors = lambda: [actor]
scope = core.capture_selected_scan_roots()
locked_paths = [item["path"] for item in scope]
# Changing the current Outliner selection must not change a locked scan scope.
core.selected_level_actors = lambda: []
progress = []
records = core.scan_selected_roots(
    rules_file,
    root_paths=locked_paths,
    progress_callback=lambda done, total: progress.append((done, total)) or True,
)
uv_records = core.scan_uv_assets(
    200.0,
    50.0,
    4.0,
    root_paths=locked_paths,
    progress_callback=lambda _done, _total: True,
)
core.selected_level_actors = original_selected_level_actors
core.all_level_actors = original_all_level_actors
if not records:
    raise RuntimeError("Transient StaticMeshActor scan returned no material slots")
if not uv_records:
    raise RuntimeError("Transient StaticMeshActor UV scan returned no mesh records")
if not progress or progress[-1] != (1, 1):
    raise RuntimeError("Scan progress callback was not completed: {}".format(progress))
actor_subsystem.destroy_actor(actor)

unreal.log(
    "[MaterialAuditorSmokeTest] PASS; module={}; slots={}; session={}".format(
        material_auditor.__file__, len(records), core.session_path()
    )
)
