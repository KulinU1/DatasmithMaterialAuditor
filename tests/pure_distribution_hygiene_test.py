# -*- coding: utf-8 -*-
"""Prevent transient caches and backup copies from entering a release package."""

from __future__ import annotations

import os


PLUGIN_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FORBIDDEN_DIRECTORY_PARTS = {
    "__pycache__",
    "saved",
    "intermediate",
    "deriveddatacache",
}
FORBIDDEN_SUFFIXES = (".pyc", ".pyo")

violations = []
for current_root, directory_names, file_names in os.walk(PLUGIN_ROOT):
    relative_root = os.path.relpath(current_root, PLUGIN_ROOT)
    path_parts = {part.lower() for part in relative_root.split(os.sep)}
    if path_parts.intersection(FORBIDDEN_DIRECTORY_PARTS):
        violations.append(relative_root)
        directory_names[:] = []
        continue
    if any("_backup_" in name.lower() for name in directory_names):
        violations.extend(
            os.path.join(relative_root, name)
            for name in directory_names
            if "_backup_" in name.lower()
        )
    for file_name in file_names:
        if file_name.lower().endswith(FORBIDDEN_SUFFIXES):
            violations.append(os.path.join(relative_root, file_name))

assert not violations, "release hygiene violations: {}".format(sorted(violations))
print("pure_distribution_hygiene_test: PASS")
