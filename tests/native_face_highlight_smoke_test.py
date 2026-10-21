# -*- coding: utf-8 -*-
"""Smoke-test the native solid triangle-face viewport overlay API."""

import unreal


library = getattr(unreal, "DatasmithMeshAuditLibrary", None)
draw = getattr(library, "draw_triangle_face_highlights", None)
draw_source_ids = getattr(library, "draw_static_mesh_triangle_highlights", None)
clear = getattr(library, "clear_triangle_face_highlights", None)
analyze_transform = getattr(library, "analyze_scene_component_transform", None)
if (
    not callable(draw)
    or not callable(draw_source_ids)
    or not callable(clear)
    or not callable(analyze_transform)
):
    raise RuntimeError("Native solid face-highlight API is unavailable")

actor_subsystem = unreal.get_editor_subsystem(unreal.EditorActorSubsystem)
cube = unreal.EditorAssetLibrary.load_asset("/Engine/BasicShapes/Cube")
if not cube:
    raise RuntimeError("Unable to load Engine Cube")

actor = actor_subsystem.spawn_actor_from_object(
    cube,
    unreal.Vector(0.0, 0.0, 0.0),
    unreal.Rotator(0.0, 0.0, 0.0),
)
if not actor:
    raise RuntimeError("Unable to spawn highlight smoke-test actor")

try:
    component = actor.get_editor_property("static_mesh_component")
    # Two component-local triangles, deliberately submitted with one winding;
    # the native function adds both windings for a camera-independent overlay.
    vertices = [
        unreal.Vector(50.0, -50.0, -50.0),
        unreal.Vector(50.0, 50.0, -50.0),
        unreal.Vector(50.0, 50.0, 50.0),
        unreal.Vector(50.0, -50.0, -50.0),
        unreal.Vector(50.0, 50.0, 50.0),
        unreal.Vector(50.0, -50.0, 50.0),
    ]
    drawn = draw(
        component,
        vertices,
        unreal.LinearColor(1.0, 0.02, 0.02, 1.0),
        0.25,
        True,
        True,
    )
    if int(drawn or 0) != 2:
        raise RuntimeError("Expected two highlighted faces, got {}".format(drawn))

    complete_drawn = draw_source_ids(
        component,
        [0, 1, 2],
        unreal.LinearColor(1.0, 0.02, 0.02, 1.0),
        0.25,
        True,
        True,
        100000,
    )
    if int(complete_drawn or 0) != 3:
        raise RuntimeError(
            "Expected three Source-ID highlighted faces, got {}".format(
                complete_drawn
            )
        )
    capped_drawn = draw_source_ids(
        component,
        [0, 1, 2],
        unreal.LinearColor(1.0, 0.02, 0.02, 1.0),
        0.25,
        True,
        True,
        1,
    )
    if int(capped_drawn or 0) != 1:
        raise RuntimeError("Source-ID highlight MaxFaces was not enforced")

    actor.set_actor_scale3d(unreal.Vector(-1.0, 2.0, 3.0))
    transform_result = analyze_transform(component)
    success = bool(transform_result.get_editor_property("success"))
    determinant = float(transform_result.get_editor_property("determinant"))
    reversing = bool(
        transform_result.get_editor_property("orientation_reversing")
    )
    if not success or determinant >= 0.0 or not reversing:
        raise RuntimeError(
            "Native component determinant audit failed: success={} det={} reversing={}".format(
                success, determinant, reversing
            )
        )
    clear(component)
finally:
    actor_subsystem.destroy_actor(actor)

unreal.log("[NativeFaceHighlightSmokeTest] PASS")
