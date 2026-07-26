"""Sway compositor backend using swaymsg IPC."""

from __future__ import absolute_import, division, print_function

import json
import os
import subprocess

from .base import CompositorBackend


class SwayBackend(CompositorBackend):
    """Window management via ``swaymsg`` IPC."""

    def __init__(self):
        self._parent_pid = None

    @classmethod
    def detect(cls):
        return bool(os.environ.get("SWAYSOCK"))

    # ------------------------------------------------------------------
    # Tree helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _query_tree():
        """Fetch the Sway tree as a parsed dict, or None on failure."""
        try:
            return json.loads(subprocess.check_output(["swaymsg", "-t", "get_tree"]))
        except Exception:
            return None

    @staticmethod
    def _collect_pids(node, pids=None):
        """Recursively collect all PIDs from the Sway tree."""
        if pids is None:
            pids = set()
        pid = node.get("pid")
        if pid:
            pids.add(pid)
        for child in node.get("nodes", []) + node.get("floating_nodes", []):
            SwayBackend._collect_pids(child, pids)
        return pids

    @staticmethod
    def _find_node_by_pid(node, pid):
        """Find the Sway tree node with the given PID."""
        if node.get("pid") == pid:
            return node
        for child in node.get("nodes", []) + node.get("floating_nodes", []):
            result = SwayBackend._find_node_by_pid(child, pid)
            if result:
                return result
        return None

    @staticmethod
    def _find_workspace_for_pid(node, pid, current_ws=None):
        """Find the workspace name containing the window with the given PID."""
        if node.get("type") == "workspace":
            current_ws = node.get("name")
        if node.get("pid") == pid:
            return current_ws
        for child in node.get("nodes", []) + node.get("floating_nodes", []):
            result = SwayBackend._find_workspace_for_pid(child, pid, current_ws)
            if result is not None:
                return result
        return None

    @staticmethod
    def _find_focused_id(node):
        """Find the container id of the focused window."""
        if node.get("focused"):
            return node.get("id")
        for child in node.get("nodes", []) + node.get("floating_nodes", []):
            result = SwayBackend._find_focused_id(child)
            if result is not None:
                return result
        return None

    @staticmethod
    def _get_node_geometry(node):
        """Extract absolute content position from a Sway tree node.

        rect is the container's absolute geometry.  window_rect is relative
        to rect and represents the actual window content area (excludes
        borders/decorations).  The absolute content position is
        rect.x + window_rect.x.
        """
        rect = node.get("rect", {})
        wr = node.get("window_rect", {})
        x = rect.get("x", 0) + wr.get("x", 0)
        y = rect.get("y", 0) + wr.get("y", 0)
        w = wr.get("width", rect.get("width", 0))
        h = wr.get("height", rect.get("height", 0))
        return (x, y, w, h)

    # ------------------------------------------------------------------
    # Backend interface
    # ------------------------------------------------------------------

    def prepare_for_draw(self):
        """Single tree query — return (terminal_geometry, focused_window_id, workspace)."""
        tree = self._query_tree()
        if tree is None:
            return None, None, None

        if self._parent_pid is None:
            sway_pids = self._collect_pids(tree)
            for pid in self._walk_ppid_chain(os.getpid()):
                if pid in sway_pids:
                    self._parent_pid = pid
                    break

        term_geo = None
        workspace = None
        if self._parent_pid is not None:
            node = self._find_node_by_pid(tree, self._parent_pid)
            if node:
                term_geo = self._get_node_geometry(node)
                workspace = self._find_workspace_for_pid(tree, self._parent_pid)
            else:
                self._parent_pid = None

        focused_id = self._find_focused_id(tree)
        return term_geo, focused_id, workspace

    def apply_no_focus(self, app_id):
        """Prevent windows with *app_id* from stealing focus on launch."""
        try:
            subprocess.run(
                ["swaymsg", "no_focus", "[app_id={}]".format(app_id)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=2,
            )
        except Exception:
            pass

    def restore_focus(self, window_id):
        """Restore focus to *window_id*."""
        if window_id is None:
            return
        try:
            subprocess.run(
                ["swaymsg", "[con_id={}]".format(window_id), "focus"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=2,
            )
        except Exception:
            pass

    def show_window(self, app_id, x, y, workspace=None):
        """Restore from scratchpad and position."""
        ws_cmd = "move to workspace {}".format(workspace) if workspace else "move to workspace current"
        try:
            subprocess.run(
                ["swaymsg", "[app_id={}]".format(app_id),
                 "{}, move absolute position {} {}".format(ws_cmd, x, y)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=2,
            )
        except Exception:
            pass

    def hide_window(self, app_id):
        """Hide the window to the scratchpad."""
        try:
            subprocess.run(
                ["swaymsg", "[app_id={}]".format(app_id),
                 "move to scratchpad"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=2,
            )
        except Exception:
            pass

    def move_window(self, app_id, x, y):
        """Move a visible window to absolute position (*x*, *y*)."""
        try:
            subprocess.run(
                ["swaymsg", "[app_id={}]".format(app_id),
                 "move absolute position {} {}".format(x, y)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=2,
            )
        except Exception:
            pass

    def move_to_workspace(self, app_id, workspace):
        """Move the window to *workspace* silently (without switching focus)."""
        if workspace is None:
            return
        try:
            subprocess.run(
                ["swaymsg", "[app_id={}]".format(app_id),
                 "move to workspace {}".format(workspace)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=2,
            )
        except Exception:
            pass
