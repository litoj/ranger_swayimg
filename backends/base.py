"""Abstract base for compositor-specific window management backends."""

from __future__ import absolute_import, division, print_function

import json
import os
import subprocess
import time


class CompositorBackend(object):
    """Interface for compositor-specific operations.

    Each backend translates generic window-management actions (show, hide,
    move, no-focus) into the IPC calls of a specific Wayland compositor.
    """

    @classmethod
    def detect(cls):
        """Return True if running under this compositor."""
        raise NotImplementedError

    def prepare_for_draw(self):
        """Return (terminal_geometry, focused_window_id, workspace, fullscreen,
            workspace_current).

        terminal_geometry: (x, y, w, h) of the terminal running ranger, or None.
        focused_window_id: opaque identifier for the currently focused window,
            or None when focus restoration is not supported.
        workspace: opaque backend-specific identifier for the workspace
            containing the terminal, or None when not supported.
        fullscreen: True when the terminal window is fullscreen.
        workspace_current: False when the terminal's workspace is not the one
            currently focused, None when unknown — view-stealing operations
            (show/restore-focus/spawn) must be gated on this.
        """
        raise NotImplementedError

    def apply_no_focus(self, app_id):
        """Prevent windows with *app_id* from stealing focus on launch."""
        pass

    def restore_focus(self, window_id):
        """Restore focus to *window_id*.  No-op when not supported."""
        pass

    def show_window(self, app_id, x, y, workspace=None):
        """Show a hidden window at absolute position (*x*, *y*) on *workspace*."""
        raise NotImplementedError

    def hide_window(self, app_id):
        """Hide the window (scratchpad / special workspace)."""
        raise NotImplementedError

    def move_window(self, app_id, x, y):
        """Move a visible window to absolute position (*x*, *y*)."""
        raise NotImplementedError

    def move_to_workspace(self, app_id, workspace):
        """Move the window to *workspace* silently (without switching focus)."""
        raise NotImplementedError

    def window_exists(self, pid):
        """Return True if a window belonging to *pid* is currently mapped."""
        raise NotImplementedError

    def wait_mapped(self, pid, timeout=2.0, alive=None):
        """Block until the window for *pid* appears; return True if it did.

        *alive* is an optional callable: when it returns False the owner
        process has died, so we bail out early instead of waiting out the
        full timeout for a window that will never map.
        """
        deadline = time.monotonic() + timeout
        while True:
            if self.window_exists(pid):
                return True
            if alive is not None and not alive():
                return False
            if time.monotonic() >= deadline:
                return False
            time.sleep(0.04)

    # ------------------------------------------------------------------
    # Shared helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _run_silent(cmd):
        """Run *cmd* silently, ignoring failures; return True on exit code 0."""
        try:
            result = subprocess.run(
                cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=2)
            return result.returncode == 0
        except Exception:
            return False

    @staticmethod
    def _json_output(cmd):
        """Run *cmd* and parse stdout as JSON; return None on any failure."""
        try:
            return json.loads(subprocess.check_output(cmd))
        except Exception:
            return None

    @staticmethod
    def _walk_ppid_chain(start_pid):
        """Yield each parent PID walking up from *start_pid*."""
        pid = start_pid
        while pid > 1:
            yield pid
            try:
                with open("/proc/{}/status".format(pid), "rb") as f:
                    for line in f:
                        if line.startswith(b"PPid:"):
                            pid = int(line.split()[1])
                            break
                    else:
                        break
            except Exception:
                break

    def _resolve_parent_pid(self, client_pids_fn):
        """Return the PID of the terminal running ranger (cached).

        Walks the ppid chain until a PID owning a compositor window is found.
        *client_pids_fn* is called only on cache miss, so collecting pids
        from the compositor costs nothing on subsequent calls.
        """
        if self._parent_pid is None:
            pids = client_pids_fn()
            for pid in self._walk_ppid_chain(os.getpid()):
                if pid in pids:
                    self._parent_pid = pid
                    break
        return self._parent_pid
