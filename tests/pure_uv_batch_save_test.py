# -*- coding: utf-8 -*-
"""Fast regression test for UV batch saving without Unreal Editor."""

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


class FakeMesh:
    def __init__(self, name):
        self.name = name
        self.modify_calls = 0

    def modify(self):
        self.modify_calls += 1


class FakeEditorAssetLibrary:
    batch_calls = []

    @classmethod
    def save_loaded_assets(cls, assets, only_if_is_dirty=True):
        cls.batch_calls.append((list(assets), only_if_is_dirty))
        return True

    @classmethod
    def save_loaded_asset(cls, *_args, **_kwargs):
        raise AssertionError("UV processing must not save each mesh separately")

    @classmethod
    def save_asset(cls, *_args, **_kwargs):
        raise AssertionError("UV processing must not save each path separately")


core.unreal.EditorAssetLibrary = FakeEditorAssetLibrary
meshes = {
    "/Game/Test/MeshA.MeshA": FakeMesh("MeshA"),
    "/Game/Test/MeshB.MeshB": FakeMesh("MeshB"),
}
core._load_static_mesh = lambda path: meshes[path]
write_calls = []
core._write_box_projection_to_static_mesh = (
    lambda source, target, scale, size, channel: write_calls.append(
        (source.name, target.name, scale, size, channel)
    )
)


def record(path, name):
    return core.UVAssetRecord(
        mesh_path=path,
        mesh_name=name,
        actor_labels=[name + "Actor"],
        components=[],
        component_count=1,
        local_dimensions=(100.0, 100.0, 100.0),
        world_dimensions=(100.0, 100.0, 100.0),
        world_scale=(1.0, 1.0, 1.0),
        major_area_m2=1.0,
        classification="small",
        projection_size=50.0,
        reason="test",
    )


records = [
    record("/Game/Test/MeshA.MeshA", "MeshA"),
    record("/Game/Test/MeshB.MeshB", "MeshB"),
]
progress = []
result = core.apply_uv_box_projection(
    records,
    0,
    progress_callback=lambda done, total: progress.append((done, total)) or True,
)

assert result["processed_meshes"] == 2, result
assert result["save_batches"] == 1, result
assert result["save_succeeded"] is True, result
assert result["cancelled"] is False, result
assert progress == [(0, 2), (1, 2), (2, 2)], progress
assert len(FakeEditorAssetLibrary.batch_calls) == 1, FakeEditorAssetLibrary.batch_calls
saved_assets, only_if_dirty = FakeEditorAssetLibrary.batch_calls[0]
assert saved_assets == [meshes[records[0].mesh_path], meshes[records[1].mesh_path]]
assert only_if_dirty is False

# Cancellation takes effect between meshes and still saves the completed part once.
FakeEditorAssetLibrary.batch_calls.clear()
write_calls.clear()
cancel_result = core.apply_uv_box_projection(
    records,
    0,
    progress_callback=lambda done, _total: done < 1,
)
assert cancel_result["processed_meshes"] == 1, cancel_result
assert cancel_result["cancelled"] is True, cancel_result
assert cancel_result["save_batches"] == 1, cancel_result
assert len(FakeEditorAssetLibrary.batch_calls) == 1, FakeEditorAssetLibrary.batch_calls
assert FakeEditorAssetLibrary.batch_calls[0][0] == [meshes[records[0].mesh_path]]

print("PASS: UV meshes are modified with progress and saved in one batch")
