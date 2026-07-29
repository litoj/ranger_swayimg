"""Compositor backend factory."""

from __future__ import absolute_import, division, print_function

from .base import CompositorBackend, DrawContext
from .sway import SwayBackend
from .hyprland import HyprlandBackend

__all__ = ["detect_backend", "CompositorBackend", "DrawContext",
           "SwayBackend", "HyprlandBackend"]


def detect_backend():
    """Auto-detect the running compositor and return a backend instance.

    Returns None if no supported compositor is detected.
    """
    for cls in (SwayBackend, HyprlandBackend):
        if cls.detect():
            return cls()
    return None
