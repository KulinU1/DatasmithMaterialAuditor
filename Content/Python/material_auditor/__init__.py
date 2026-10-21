# -*- coding: utf-8 -*-
"""Public entry points for the editor tool."""


def show_window(*args, **kwargs):
    # Keep non-UI automation usable without importing Qt until the window opens.
    from .ui import show_window as _show_window

    return _show_window(*args, **kwargs)

__all__ = ["show_window"]
