"""Abstract base for compositor-specific window management backends."""

from __future__ import absolute_import, division, print_function


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
        """Return (terminal_geometry, focused_window_id, workspace) in one query.

        terminal_geometry: (x, y, w, h) of the terminal running ranger, or None.
        focused_window_id: opaque identifier for the currently focused window,
            or None when focus restoration is not supported.
        workspace: opaque backend-specific identifier for the workspace
            containing the terminal, or None when not supported.
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

    # ------------------------------------------------------------------
    # Shared helpers
    # ------------------------------------------------------------------

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
