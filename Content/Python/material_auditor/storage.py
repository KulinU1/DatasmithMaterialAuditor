# -*- coding: utf-8 -*-
"""Reliable user-state and report storage with a per-user fallback.

Project ``Saved`` is normally writable, but Perforce mappings, copied projects,
or Windows ACLs can make individual state files read-only.  All writes therefore
try the project Saved directory first and transparently fall back to LocalAppData.
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from typing import Any, Dict, Iterable, List, Optional


TOOL_NAME = "DatasmithMaterialAuditor"
VENDOR_NAME = "Kulin"


def _normal_path(value: str) -> str:
    return os.path.normpath(os.path.abspath(value))


def _unreal_paths() -> Dict[str, str]:
    try:
        import unreal

        project_dir = unreal.Paths.convert_relative_path_to_full(
            unreal.Paths.project_dir()
        )
        project_saved = unreal.Paths.convert_relative_path_to_full(
            unreal.Paths.project_saved_dir()
        )
        return {
            "project_dir": _normal_path(project_dir),
            "project_saved": _normal_path(project_saved),
        }
    except Exception:
        project_dir = _normal_path(os.getcwd())
        return {
            "project_dir": project_dir,
            "project_saved": _normal_path(os.path.join(project_dir, "Saved")),
        }


def _project_key(project_dir: str) -> str:
    value = os.path.normcase(_normal_path(project_dir)).encode("utf-8")
    return hashlib.sha1(value).hexdigest()[:12]


def candidate_directories(
    project_saved: Optional[str] = None,
    project_dir: Optional[str] = None,
    local_app_data: Optional[str] = None,
) -> List[str]:
    paths = _unreal_paths()
    project_saved = _normal_path(project_saved or paths["project_saved"])
    project_dir = _normal_path(project_dir or paths["project_dir"])
    local_app_data = _normal_path(
        local_app_data
        or os.environ.get("LOCALAPPDATA")
        or tempfile.gettempdir()
    )
    values = [
        os.path.join(project_saved, TOOL_NAME),
        os.path.join(
            local_app_data,
            VENDOR_NAME,
            TOOL_NAME,
            _project_key(project_dir),
        ),
    ]
    result: List[str] = []
    for value in values:
        value = _normal_path(value)
        if value not in result:
            result.append(value)
    return result


def _ensure_directory(directory: str) -> None:
    os.makedirs(directory, exist_ok=True)


def writable_directory(**overrides: str) -> str:
    errors = []
    for directory in candidate_directories(**overrides):
        try:
            _ensure_directory(directory)
            handle, probe = tempfile.mkstemp(prefix=".dma_write_probe_", dir=directory)
            os.close(handle)
            os.unlink(probe)
            return directory
        except Exception as exc:
            errors.append("{}: {}".format(directory, exc))
    raise PermissionError("插件没有可写的状态目录：{}".format(" | ".join(errors)))


def readable_path(filename: str, **overrides: str) -> str:
    """Return the newest existing copy, or the preferred future write path."""
    existing = []
    for directory in candidate_directories(**overrides):
        path = os.path.join(directory, filename)
        if os.path.isfile(path):
            try:
                existing.append((os.path.getmtime(path), path))
            except OSError:
                existing.append((0.0, path))
    if existing:
        return max(existing, key=lambda item: item[0])[1]
    return os.path.join(writable_directory(**overrides), filename)


def _atomic_write_text(path: str, text: str, encoding: str = "utf-8") -> None:
    directory = os.path.dirname(path)
    _ensure_directory(directory)
    handle, temporary = tempfile.mkstemp(prefix=".dma_", suffix=".tmp", dir=directory)
    try:
        with os.fdopen(handle, "w", encoding=encoding, newline="") as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    except Exception:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise


def write_text(filename: str, text: str, **overrides: str) -> str:
    errors = []
    for directory in candidate_directories(**overrides):
        path = os.path.join(directory, filename)
        try:
            _atomic_write_text(path, text)
            return path
        except Exception as exc:
            errors.append("{}: {}".format(path, exc))
    raise PermissionError("插件状态写入失败：{}".format(" | ".join(errors)))


def write_json(
    filename: str,
    value: Any,
    compact: bool = False,
    **overrides: str,
) -> str:
    options = {"ensure_ascii": False}
    if compact:
        options["separators"] = (",", ":")
    else:
        options["indent"] = 2
    return write_text(filename, json.dumps(value, **options), **overrides)


def read_json(filename: str, default: Any = None, **overrides: str) -> Any:
    path = readable_path(filename, **overrides)
    if not os.path.isfile(path):
        return default
    with open(path, "r", encoding="utf-8") as stream:
        return json.load(stream)


def report_path(filename: str, **overrides: str) -> str:
    return os.path.join(writable_directory(**overrides), filename)

