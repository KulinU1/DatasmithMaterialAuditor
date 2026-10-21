# -*- coding: utf-8 -*-
"""Fast scan-scope regression test runnable without Unreal Editor."""

import pathlib
import sys
import types


sys.dont_write_bytecode = True


try:
    import unreal  # type: ignore  # noqa: F401
except ImportError:
    sys.modules["unreal"] = types.ModuleType("unreal")
else:
    raise RuntimeError("Run this regression test with regular Python, not inside UE.")


PYTHON_ROOT = pathlib.Path(__file__).resolve().parents[1] / "Content" / "Python"
sys.path.insert(0, str(PYTHON_ROOT))
from material_auditor import core


class FakeActor:
    def __init__(self, path, parent=None):
        self.path = path
        self.parent = parent

    def get_path_name(self):
        return self.path

    def get_actor_label(self):
        return self.path.rsplit(".", 1)[-1]

    def get_attach_parent_actor(self):
        return self.parent


root = FakeActor("/Game/Test.Root")
child = FakeActor("/Game/Test.Child", root)
grandchild = FakeActor("/Game/Test.Grandchild", child)
core.selected_level_actors = lambda: [root, child, grandchild]
core.all_level_actors = lambda: [root, child, grandchild]

scope = core.capture_selected_scan_roots()
assert scope == [{"path": root.path, "label": "Root"}], scope

# A locked path must survive later selection changes caused by filter buttons.
core.selected_level_actors = lambda: [child, grandchild]
root_paths, actors = core._resolve_scan_scope([root.path])
assert root_paths == {root.path}
assert actors == [root, child, grandchild]
assert core._is_under_root(grandchild, root_paths)

many_roots = [FakeActor("/Game/Test.Root{}".format(index)) for index in range(65)]
core.selected_level_actors = lambda: many_roots
try:
    core.capture_selected_scan_roots()
except RuntimeError as exc:
    assert "超过安全上限" in str(exc)
else:
    raise AssertionError("More than 64 selected roots must be rejected")

# UV scanning deliberately accepts the current selection directly, including
# more than the material scanner's 64-root safety limit.
uv_scope = core.capture_selected_uv_roots()
assert len(uv_scope) == len(many_roots), len(uv_scope)

try:
    core._report_scan_progress(lambda _done, _total: False, 1, 2)
except core.ScanCancelledError:
    pass
else:
    raise AssertionError("Cancellation callback did not stop the scan")

assert core._classify_from_properties("BLEND_OPAQUE", "MSM_DEFAULT_LIT", "MD_SURFACE")[0] == "non_glass"
assert core._classify_from_properties("BLEND_TRANSLUCENT", "MSM_DEFAULT_LIT", "MD_SURFACE")[0] == "glass"

uv_common = dict(
    mesh_path="/Game/Test/Mesh.Mesh",
    mesh_name="Mesh",
    actor_labels=["Actor"],
    components=[],
    component_count=1,
    local_dimensions=(100.0, 100.0, 100.0),
    world_dimensions=(100.0, 100.0, 100.0),
    major_area_m2=1.0,
    classification="small",
    projection_size=50.0,
    reason="test",
)
uv_a = core.UVAssetRecord(world_scale=(1.0, 1.0, 1.0), **uv_common)
uv_b = core.UVAssetRecord(world_scale=(2.0, 1.0, 1.0), **uv_common)
assert core._uv_projection_signature(uv_a) == (50.0, 50.0, 50.0)
assert core._uv_projection_signature(uv_b) == (25.0, 50.0, 50.0)


class FakeMaterial:
    def __init__(self, path):
        self.path = path

    def get_path_name(self):
        return self.path


synced_materials = []
core.load_material = lambda path: (
    None if "Missing" in path else FakeMaterial(path)
)
core.unreal.EditorAssetLibrary = types.SimpleNamespace(
    sync_browser_to_objects=lambda paths: synced_materials.extend(paths)
)
locator_result = core.sync_content_browser_to_materials(
    ["/Game/Test/M_A.M_A", "/Game/Test/M_A.M_A", "/Game/Test/Missing.Missing"]
)
assert locator_result["located"] == 1, locator_result
assert locator_result["missing"] == ["/Game/Test/Missing.Missing"], locator_result
assert synced_materials == ["/Game/Test/M_A.M_A"], synced_materials

print("PASS: scan scope, cancellation, material locator, and in-place UV signatures")
