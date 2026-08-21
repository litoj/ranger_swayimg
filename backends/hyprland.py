"""Hyprland compositor backend using hyprctl IPC."""

from __future__ import absolute_import, division, print_function

import os

from .base import CompositorBackend, DrawContext


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

    def _get_clients(self):
        return self._json_output(["hyprctl", "clients", "-j"])

    def _dispatch(self, *args):
        self._run_silent(["hyprctl", "dispatch"] + list(args))

    # ------------------------------------------------------------------
    # Backend interface
    # ------------------------------------------------------------------

    def prepare_for_draw(self) -> DrawContext:
        clients = self._get_clients()
        if clients is None:
            return DrawContext(None, None, None, False, None)

        parent = self._resolve_parent_pid(lambda: {c.get("pid") for c in clients if c.get("pid")})
        if parent is None:
            return DrawContext(None, None, None, False, None)

        for c in clients:
            if c.get("pid") == parent:
                at = c.get("at", [0, 0])
                size = c.get("size", [0, 0])
                ws = c.get("workspace", {}).get("id")
                active = self._json_output(["hyprctl", "activeworkspace", "-j"])
                active_id = active.get("id") if active else None
                ws_current = (None if ws is None or active_id is None
                              else ws == active_id)
                return DrawContext((at[0], at[1], size[0], size[1]),
                                   None, ws, bool(c.get("fullscreen")), ws_current)

        self._parent_pid = None
        return DrawContext(None, None, None, False, None)

    def window_exists(self, pid):
        clients = self._get_clients()
        if clients is None:
            return False
        return any(c.get("pid") == pid for c in clients)

    def apply_no_focus(self, app_id):
        """Attempt to add a no_focus window rule at runtime.

        This may not work on all Hyprland versions.  Users should also add
        the rule manually to their config as a reliable fallback.
        """
        self._run_silent(["hyprctl", "keyword", "windowrulev2",
                          "no_focus,class:^{}$".format(app_id)])

    def show_window(self, app_id, x, y, workspace=None):
        """Restore from special workspace, then move to position."""
        self.move_to_workspace(app_id, workspace)
        self.move_window(app_id, x, y)

    def hide_window(self, app_id):
        self._dispatch("movetoworkspacesilent",
                       "special:{},class:^{}$".format(self.SPECIAL_WORKSPACE, app_id))

    def move_window(self, app_id, x, y):
        # hyprctl has no absolute move; movewindowpixel takes deltas.
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
                self._dispatch("movewindowpixel", "{} {},class:^{}$".format(dx, dy, app_id))
                return

    def move_to_workspace(self, app_id, workspace):
        if workspace is not None:
            self._dispatch("movetoworkspacesilent", "{},class:^{}$".format(workspace, app_id))
