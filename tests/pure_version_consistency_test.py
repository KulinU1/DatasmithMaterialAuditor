# -*- coding: utf-8 -*-
"""Ensure public release metadata remains synchronized."""

import json
import os
import re
import sys


PLUGIN_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PYTHON_ROOT = os.path.join(PLUGIN_ROOT, "Content", "Python")
if PYTHON_ROOT not in sys.path:
    sys.path.insert(0, PYTHON_ROOT)

from material_auditor import version


with open(
    os.path.join(PLUGIN_ROOT, "DatasmithMaterialAuditor.uplugin"),
    "r",
    encoding="utf-8",
) as stream:
    manifest = json.load(stream)

assert manifest["Version"] == version.PLUGIN_VERSION_INT, manifest["Version"]
assert manifest["VersionName"] == version.PLUGIN_VERSION, manifest["VersionName"]

with open(os.path.join(PLUGIN_ROOT, "README.md"), "r", encoding="utf-8") as stream:
    readme = stream.read()
assert re.search(
    r"^版本：{}\s*$".format(re.escape(version.PLUGIN_VERSION)),
    readme,
    re.MULTILINE,
), "README version is not synchronized"

print("pure_version_consistency_test: PASS")

