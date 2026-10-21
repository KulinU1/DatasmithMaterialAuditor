# -*- coding: utf-8 -*-
"""Regression coverage for large material-slot scenes without Unreal Editor."""

import importlib
import inspect
import pathlib
import sys
import types


sys.dont_write_bytecode = True
sys.modules["unreal"] = types.ModuleType("unreal")
unreal = sys.modules["unreal"]
unreal.ActorComponent = object

PLUGIN_ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PLUGIN_ROOT / "Content" / "Python"))
core = importlib.import_module("material_auditor.core")
ui = importlib.import_module("material_auditor.ui")


class FakeMaterial:
    def __init__(self, path):
        self.path = path

    def get_path_name(self):
        return self.path


class FakeComponent:
    def __init__(self, name, material):
        self.name = name
        self.material = material
        self.read_count = 0
        self.write_count = 0

    def get_name(self):
        return self.name

    def get_material(self, _index):
        self.read_count += 1
        return self.material

    def modify(self):
        return None

    def set_material(self, _index, material):
        self.material = material
        self.write_count += 1


class FakeActor:
    def __init__(self, path, component):
        self.path = path
        self.component = component
        self.component_scan_count = 0

    def get_path_name(self):
        return self.path

    def get_components_by_class(self, _component_class):
        self.component_scan_count += 1
        return [self.component]


material = FakeMaterial("/Game/Test/M.M")
component = FakeComponent("Mesh", material)
actor = FakeActor("/Game/Test.Level:PersistentLevel.Actor", component)
all_actor_calls = 0


def fake_all_level_actors():
    global all_actor_calls
    all_actor_calls += 1
    return [actor]


core.all_level_actors = fake_all_level_actors


def make_record(index):
    return core.SlotRecord(
        actor_path=actor.path,
        actor_label="Actor",
        component_name=component.name,
        slot_index=index,
        slot_name="Slot_{}".format(index),
        material_path=material.path,
        material_name="M",
        base_material_path="/Game/Test/Base.Base",
        blend_mode="BLEND_OPAQUE",
        shading_model="MSM_DEFAULT_LIT",
        material_domain="MD_SURFACE",
        writes_world_normal=True,
        writes_scene_depth=True,
        two_sided=True,
        normal_connected=True,
        classification="non_glass",
        confidence=100,
        reason="test",
    )


records = [make_record(index) for index in range(21269)]
resolver = core._SlotResolver()
snapshot = core._snapshot(records, resolver)
materials = core._materials_from_records(records, resolver)

assert all_actor_calls == 1, all_actor_calls
assert len(snapshot) == len(records), len(snapshot)
assert materials == [material], materials
assert len(resolver.component_cache) == 1, resolver.component_cache
assert component.read_count == len(records) + 1, component.read_count

# Undo/restore also caches actors, components, and loaded materials.
load_calls = 0


def fake_load_material(_path):
    global load_calls
    load_calls += 1
    return material


core.load_material = fake_load_material
restored, restore_errors = core._set_slots(snapshot)
assert restored == len(records), restored
assert not restore_errors, restore_errors
assert all_actor_calls == 2, all_actor_calls
assert load_calls == 1, load_calls
assert actor.component_scan_count == 2, actor.component_scan_count

# The table is compacted to unique materials while filters still operate on
# every slot record.
window_state = types.SimpleNamespace(records=records, material_rows=[])
ui.MaterialAuditorWindow._build_material_rows(window_state)
assert len(window_state.material_rows) == 1, len(window_state.material_rows)
assert window_state.material_rows[0]["slot_count"] == len(records)
assert ui.MaterialAuditorWindow._record_matches_filter(records[0], "non_glass")
assert not ui.MaterialAuditorWindow._record_matches_filter(records[0], "glass")

uv_scan_source = inspect.getsource(ui.MaterialAuditorWindow.scan_uv)
assert "_ensure_scan_scope" not in uv_scan_source
assert "root_paths=None" in uv_scan_source

# Standalone two-sided processing must not build a redundant slot snapshot.
core._read_session = lambda: {"original_snapshot": [{"old": True}]}
written = []
core._write_session = lambda session: written.append(dict(session))
session = core._start_processing_session(
    records, resolver, snapshot_slots=False
)
assert session["original_snapshot"] == []
assert written and written[-1]["original_snapshot"] == []

print(
    "PASS: 21269 slots use one actor index, one component cache entry, "
    "and one material table row"
)
