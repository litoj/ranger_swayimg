"""Sway compositor backend using swaymsg IPC."""

from __future__ import absolute_import, division, print_function

import os

from .base import CompositorBackend, DrawContext


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

    def _query_tree(self):
        return self._json_output(["swaymsg", "-t", "get_tree"])

    @staticmethod
    def _collect_pids(node, pids=None):
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
        if node.get("pid") == pid:
            return node
        for child in node.get("nodes", []) + node.get("floating_nodes", []):
            result = SwayBackend._find_node_by_pid(child, pid)
            if result:
                return result
        return None

    @staticmethod
    def _find_workspace_for_pid(node, pid, current_ws=None):
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
        if node.get("focused"):
            return node.get("id")
        for child in node.get("nodes", []) + node.get("floating_nodes", []):
            result = SwayBackend._find_focused_id(child)
            if result is not None:
                return result
        return None

    @staticmethod
    def _find_focused_workspace(node):
        focused = None
        for n in SwayBackend._walk_nodes(node):
            if n.get("focused") and n.get("type") in ("con", "floating_con"):
                focused = n.get("id")
                break
        if focused is None:
            return None
        for ws in SwayBackend._walk_nodes(node):
            if ws.get("type") == "workspace" and any(
                    n.get("id") == focused for n in SwayBackend._walk_nodes(ws)):
                return ws.get("name")
        return None

    @staticmethod
    def _walk_nodes(node):
        yield node
        for child in node.get("nodes", []) + node.get("floating_nodes", []):
            for n in SwayBackend._walk_nodes(child):
                yield n

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

    def _swaymsg(self, *args):
        self._run_silent(["swaymsg"] + list(args))

    # ------------------------------------------------------------------
    # Backend interface
    # ------------------------------------------------------------------

    def prepare_for_draw(self) -> DrawContext:
        """Single tree query — one DrawContext snapshot."""
        tree = self._query_tree()
        if tree is None:
            return DrawContext(None, None, None, False, None)

        term_geo = None
        workspace = None
        fullscreen = False
        if self._resolve_parent_pid(lambda: self._collect_pids(tree)) is not None:
            node = self._find_node_by_pid(tree, self._parent_pid)
            if node:
                term_geo = self._get_node_geometry(node)
                workspace = self._find_workspace_for_pid(tree, self._parent_pid)
                fullscreen = bool(node.get("fullscreen_mode"))
            else:
                self._parent_pid = None

        # View-stealing operations (show/restore focus) are only safe while
        # the terminal's workspace is the one in front of the user.
        focused_ws = self._find_focused_workspace(tree)
        ws_current = (None if workspace is None or focused_ws is None
                      else workspace == focused_ws)

        focused_id = self._find_focused_id(tree)
        return DrawContext(term_geo, focused_id, workspace, fullscreen, ws_current)

    def window_exists(self, pid):
        tree = self._query_tree()
        return tree is not None and self._find_node_by_pid(tree, pid) is not None

    def apply_no_focus(self, app_id):
        self._swaymsg("no_focus", "[app_id={}]".format(app_id))

    def restore_focus(self, window_id):
        if window_id is not None:
            self._swaymsg("[con_id={}]".format(window_id), "focus")

    def show_window(self, app_id, x, y, workspace=None):
        """Restore from scratchpad and position.

        MUST be one atomic command with the position move FIRST and the
        workspace move LAST.  Sway realizes a window onto the workspace
        currently in front of the user the moment it applies an absolute
        position to it (for a scratchpad window this beats a queued or
        preceding workspace move; for a window on another workspace it
        yanks the window over), so any order but this drops the preview in
        front of whatever the user is doing.  With the workspace move last
        the final state is always the named workspace — verified silently
        and correctly even while another workspace is showing.  Without a
        known workspace the window stays hidden: showing it wherever the
        user happens to be is exactly what must not happen.
        """
        if workspace is not None:
            self._swaymsg("[app_id={}]".format(app_id),
                          "move absolute position {} {}, move to workspace {}".format(x, y, workspace))

    def hide_window(self, app_id):
        """Hide the window to the scratchpad."""
        self._swaymsg("[app_id={}]".format(app_id), "move to scratchpad")

    def move_window(self, app_id, x, y):
        """Move a visible window to (*x*, *y*).

        Only call while the window's workspace is the one in front of the
        user: sway applies an absolute position by pulling a floating
        window onto the CURRENT workspace, so firing this while ranger's
        workspace is invisible yanks the preview onto whatever the user is
        looking at.
        """
        self._swaymsg("[app_id={}]".format(app_id),
                      "move absolute position {} {}".format(x, y))

    def move_to_workspace(self, app_id, workspace):
        if workspace is not None:
            self._swaymsg("[app_id={}]".format(app_id),
                          "move to workspace {}".format(workspace))
