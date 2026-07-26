"""Hyprland compositor backend using hyprctl IPC."""

from __future__ import absolute_import, division, print_function

import json
import os
import subprocess

from .base import CompositorBackend


class HyprlandBackend(CompositorBackend):
    """Window management via ``hyprctl`` IPC.

    Limitations compared to Sway:

    * **no_focus** cannot be set at runtime reliably — users must add
      ``windowrulev2 = no_focus,class:^<app_id>$`` to their Hyprland config.
    * **focus restoration** after showing the preview window is not
      implemented (``prepare_for_draw`` returns ``None`` for focus id).
    """

    SPECIAL_WORKSPACE = "ranger-preview"

    def __init__(self):
        self._parent_pid = None

    @classmethod
    def detect(cls):
        return bool(os.environ.get("HYPRLAND_INSTANCE_SIGNATURE"))

    # ------------------------------------------------------------------
    # hyprctl helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _get_clients():
        """Return parsed ``hyprctl clients -j`` output, or None."""
        try:
            return json.loads(subprocess.check_output(["hyprctl", "clients", "-j"]))
        except Exception:
            return None

    @staticmethod
    def _dispatch(args):
        """Run ``hyprctl dispatch`` with *args*, ignoring errors."""
        try:
            subprocess.run(
                ["hyprctl", "dispatch"] + args,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=2,
            )
        except Exception:
            pass

    @staticmethod
    def _keyword(args):
        """Run ``hyprctl keyword`` with *args*, ignoring errors."""
        try:
            subprocess.run(
                ["hyprctl", "keyword"] + args,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=2,
            )
        except Exception:
            pass

    # ------------------------------------------------------------------
    # Backend interface
    # ------------------------------------------------------------------

    def prepare_for_draw(self):
        """Return (terminal_geometry, None, workspace_id) — focus not supported."""
        clients = self._get_clients()
        if clients is None:
            return None, None, None

        if self._parent_pid is None:
            client_pids = set(c.get("pid") for c in clients if c.get("pid"))
            for pid in self._walk_ppid_chain(os.getpid()):
                if pid in client_pids:
                    self._parent_pid = pid
                    break

        if self._parent_pid is None:
            return None, None, None

        for c in clients:
            if c.get("pid") == self._parent_pid:
                at = c.get("at", [0, 0])
                size = c.get("size", [0, 0])
                ws = c.get("workspace", {}).get("id")
                return (at[0], at[1], size[0], size[1]), None, ws

        self._parent_pid = None
        return None, None, None

    def apply_no_focus(self, app_id):
        """Attempt to add a no_focus window rule at runtime.

        This may not work on all Hyprland versions.  Users should also add
        the rule manually to their config as a reliable fallback.
        """
        self._keyword([
            "windowrulev2",
            "no_focus,class:^{}$".format(app_id),
        ])

    def show_window(self, app_id, x, y, workspace=None):
        """Restore from special workspace, then move to position."""
        if workspace is not None:
            self._dispatch([
                "movetoworkspacesilent",
                "{},class:^{}$".format(workspace, app_id),
            ])
        self.move_window(app_id, x, y)

    def hide_window(self, app_id):
        """Hide the window to a special workspace."""
        self._dispatch([
            "movetoworkspacesilent",
            "special:{},class:^{}$".format(self.SPECIAL_WORKSPACE, app_id),
        ])

    def move_window(self, app_id, x, y):
        """Move window to absolute position using delta-based movewindowpixel."""
        clients = self._get_clients()
        if clients is None:
            return

        for c in clients:
            if c.get("class") == app_id:
                at = c.get("at", [0, 0])
                dx = x - at[0]
                dy = y - at[1]
                if dx == 0 and dy == 0:
                    return
                self._dispatch([
                    "movewindowpixel",
                    "{} {},class:^{}$".format(dx, dy, app_id),
                ])
                return

    def move_to_workspace(self, app_id, workspace):
        """Move the window to *workspace* silently (without switching focus)."""
        if workspace is None:
            return
        self._dispatch([
            "movetoworkspacesilent",
            "{},class:^{}$".format(workspace, app_id),
        ])
