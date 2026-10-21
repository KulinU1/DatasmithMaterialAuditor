# -*- coding: utf-8 -*-
"""Pure-Python 0.9.5 result-adapter and complete-region metadata regression."""

from __future__ import annotations

import os
import sys
import types


sys.modules["unreal"] = types.SimpleNamespace(
    StaticMeshComponent=type("StaticMeshComponent", (), {}),
    log=lambda _message: None,
    log_warning=lambda _message: None,
)

PLUGIN_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PYTHON_ROOT = os.path.join(PLUGIN_ROOT, "Content", "Python")
if PYTHON_ROOT not in sys.path:
    sys.path.insert(0, PYTHON_ROOT)

from material_auditor import mesh_quality


group = mesh_quality.MeshAuditGroup(
    mesh_path="/Game/Test/SM_Inward",
    mesh_name="SM_Inward",
    world_scale=(1.0, 1.0, 1.0),
    uses=[
        mesh_quality.MeshComponentUse(
            actor_path="/Temp/InwardActor",
            actor_label="InwardActor",
            component_path="/Temp/InwardActor.Mesh",
            component_name="Mesh",
        )
    ],
    mesh=object(),
)
summary = {
    "mesh_path": group.mesh_path,
    "triangle_count": 4,
    "uv_channel_count": 1,
    "lod_index": 0,
    "normal_data_source": "source_mesh_description_lod0",
    "analysis_milliseconds": 0.25,
}
closed_shell_issue = {
    "issue_type": "CLOSED_SHELL_INWARD",
    "issue_code": "CLOSED_SHELL_INWARD",
    "detail_code": "NEGATIVE_SIGNED_VOLUME",
    "severity": "ERROR",
    "count": 4,
    "lod_index": 0,
    "region_id": "lod0-region-0",
    "region_index": 0,
    "error_triangle_ids": [0, 1, 2, 3],
    "triangle_id_space": "source_mesh_description_lod0",
    "local_bounds_min": (0.0, 0.0, 0.0),
    "local_bounds_max": (100.0, 100.0, 100.0),
    "samples_truncated": True,
    "triangle_ids": [0],
    "sample_vertices": [
        (0.0, 0.0, 0.0),
        (100.0, 0.0, 0.0),
        (0.0, 100.0, 0.0),
    ],
    "sample_face_normals": [(0.0, 0.0, 1.0)],
}
row = mesh_quality._native_issue_row(group, closed_shell_issue, summary)
assert row.issue_code == "CLOSED_SHELL_INWARD"
assert row.detail_code == "NEGATIVE_SIGNED_VOLUME"
assert row.severity == "error"
assert row.category == "normal"
assert row.lod_index == 0
assert row.region_id == "lod0-region-0"
assert row.region_index == 0
assert row.error_triangle_ids == [0, 1, 2, 3]
assert row.triangle_ids == [0]
assert row.triangle_id_space == "source_mesh_description_lod0"
assert row.local_bounds_min == (0.0, 0.0, 0.0)
assert row.local_bounds_max == (100.0, 100.0, 100.0)
assert row.samples_truncated is True
assert row.details["normal_data_source"] == "source_mesh_description_lod0"
assert row.details["native_region_fields"] is True

# Even if a stale/native bridge reports an elevated severity, an open surface
# only has unknown global direction and must remain informational in the UI.
open_issue = dict(closed_shell_issue)
open_issue.update(
    {
        "issue_type": "OPEN_MESH_DIRECTION_UNKNOWN",
        "issue_code": "OPEN_MESH_DIRECTION_UNKNOWN",
        "detail_code": "OPEN_BOUNDARY",
        "severity": "ERROR",
        "count": 2,
        "error_triangle_ids": [0, 1],
    }
)
open_row = mesh_quality._native_issue_row(group, open_issue, summary)
assert open_row.severity == "info"
assert open_row.category == "normal"

# Closed-shell direction coverage is independent of issue rows.  In
# particular, an abstained shell must survive into UI/report summary even when
# the native audit emits no problem row for that shell.
group.audit_result_summary = {
    "closed_shell_direction_candidate_count": 3,
    "closed_shell_direction_evaluated_count": 2,
    "closed_shell_direction_abstained_count": 1,
    "direction_abstention_reasons": ["nested_shell_ambiguity"],
}
coverage = mesh_quality.summarize_audit_groups([group])
assert coverage["analyzed_group_count"] == 1
assert coverage["closed_shell_direction_candidate_count"] == 3
assert coverage["closed_shell_direction_evaluated_count"] == 2
assert coverage["closed_shell_direction_abstained_count"] == 1
assert coverage["direction_abstention_reasons"] == ["nested_shell_ambiguity"]
report_summary = mesh_quality._report_summary([], coverage)
assert report_summary["row_count"] == 0
assert report_summary["closed_shell_direction_abstained_count"] == 1
assert report_summary["direction_abstention_reasons"] == [
    "nested_shell_ambiguity"
]

print("pure_mesh_quality_v095_adapter_test: PASS")
