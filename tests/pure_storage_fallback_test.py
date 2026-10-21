# -*- coding: utf-8 -*-
"""Pure-Python regression tests for state/report storage fallback."""

import json
import os
import sys
import tempfile


PLUGIN_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PYTHON_ROOT = os.path.join(PLUGIN_ROOT, "Content", "Python")
if PYTHON_ROOT not in sys.path:
    sys.path.insert(0, PYTHON_ROOT)

from material_auditor import storage


with tempfile.TemporaryDirectory() as temporary:
    project_dir = os.path.join(temporary, "Project")
    project_saved = os.path.join(project_dir, "Saved")
    local_data = os.path.join(temporary, "LocalAppData")
    os.makedirs(project_saved)

    first = storage.write_json(
        "state.json",
        {"value": 1},
        project_saved=project_saved,
        project_dir=project_dir,
        local_app_data=local_data,
    )
    assert first.startswith(os.path.join(project_saved, storage.TOOL_NAME)), first
    assert storage.read_json(
        "state.json",
        project_saved=project_saved,
        project_dir=project_dir,
        local_app_data=local_data,
    ) == {"value": 1}

with tempfile.TemporaryDirectory() as temporary:
    project_dir = os.path.join(temporary, "Project")
    os.makedirs(project_dir)
    blocked_saved = os.path.join(project_dir, "Saved")
    with open(blocked_saved, "w", encoding="utf-8") as stream:
        stream.write("this file intentionally blocks directory creation")
    local_data = os.path.join(temporary, "LocalAppData")

    fallback = storage.write_json(
        "state.json",
        {"value": 2},
        project_saved=blocked_saved,
        project_dir=project_dir,
        local_app_data=local_data,
    )
    expected_root = os.path.join(local_data, "Kulin", storage.TOOL_NAME)
    assert fallback.startswith(expected_root), fallback
    with open(fallback, "r", encoding="utf-8") as stream:
        assert json.load(stream) == {"value": 2}

print("pure_storage_fallback_test: PASS")

