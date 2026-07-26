# Display image previews in ranger using swayimg (Wayland image viewer).
#
# Instead of restarting swayimg for every preview, a single instance is kept
# alive and controlled via a command file + SIGUSR1.  The companion swayimg
# Lua plugin (using sai.swi) lives next to this file in ranger_preview.lua.
#
# The preview window stays visible even when ranger loses focus.  It only hides
# when the cursor moves to a non-image file.  All window management is handled
# by the Python backend layer (see the ``backends`` package).
#
# Requirements:
#   - swayimg with sai.swi plugin (https://github.com/litoj/sai.swi)
#   - Sway or Hyprland compositor
#
# For Sway, no configuration is needed.
# For Hyprland, add ``windowrulev2 = no_focus,class:^ranger-swayimg$``
# to ``hyprland.conf`` so the preview window never steals focus.

from __future__ import absolute_import, division, print_function

import os
import signal
import subprocess
import threading
import time

from ranger.ext.img_display import ImageDisplayer, get_font_dimensions, register_image_displayer

from .backends import detect_backend


@register_image_displayer("swayimg")
class SwayimgImageDisplayer(ImageDisplayer):
    """Image preview using a persistent swayimg instance."""

    APP_ID = "ranger-swayimg"

    def __init__(self):
        self.process = None
        self.cmd_file = None
        self.last_geometry = None
        self.last_path = None
        self._hidden = True
        self._hide_timer = None
        self._last_workspace = None
        self._last_image_time = 0.0
        self._backend = detect_backend()

    def _running(self):
        return self.process is not None and self.process.poll() is None

    def _compute_geometry(self, start_x, start_y, width, height):
        """Compute preview pixel geometry and capture the focused window.

        Returns (geometry_tuple, focus_id, workspace) where geometry is
        (x, y, w, h), focus_id is an opaque backend-specific identifier
        for the currently focused window (or None), and workspace is an
        opaque backend-specific identifier for the workspace containing
        the terminal (or None).
        """
        if self._backend is None:
            return None, None, None

        try:
            font_width, font_height = get_font_dimensions()
        except Exception:
            return None, None, None

        term_geo, focus_id, workspace = self._backend.prepare_for_draw()
        if term_geo is None:
            return None, None, None

        term_x, term_y = term_geo[0], term_geo[1]

        preview_x = term_x + start_x * font_width
        preview_y = term_y + start_y * font_height
        preview_w = width * font_width
        preview_h = height * font_height

        preview_x = max(0, preview_x)
        preview_y = max(0, preview_y)

        return (preview_x, preview_y, preview_w, preview_h), focus_id, workspace

    def _start(self, path, geometry):
        """Launch swayimg and the companion Lua plugin."""
        working_dir = self.working_dir or os.environ.get("XDG_RUNTIME_DIR") or "/tmp"
        self.cmd_file = os.path.join(
            working_dir,
            "ranger-swayimg-{}.cmd".format(os.getpid())
        )
        self._write_cmd_file(
            "show", path,
            (geometry[2], geometry[3]) if geometry else None)

        lua_script = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ranger_preview.lua")
        cmd = ["swayimg", "--appid", self.APP_ID, "--config", lua_script]
        if geometry is not None:
            x, y, w, h = geometry
            cmd.extend(["--position", "{},{}".format(x, y)])
            cmd.extend(["--size", "{},{}".format(w, h)])
        cmd.append(path)

        env = os.environ.copy()
        env["RANGER_SWAYIMG_CMD"] = self.cmd_file

        # Ensure swayimg never steals focus (blocking — must complete before launch).
        if self._backend is not None:
            self._backend.apply_no_focus(self.APP_ID)

        # pylint: disable=consider-using-with
        try:
            self.process = subprocess.Popen(
                cmd,
                cwd=self.working_dir,
                env=env,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except OSError:
            self.process = None
        self.last_geometry = geometry
        self._last_workspace = None
        self._last_image_time = 0.0

    def _write_cmd_file(self, action, arg="", window_size=None):
        """Atomically write the command file so Lua never reads a partial file."""
        if self.cmd_file is None:
            return False
        tmp = self.cmd_file + ".tmp"
        with open(tmp, "w") as f:
            f.write(action + "\n")
            if arg:
                f.write(arg + "\n")
            if window_size is not None:
                w, h = window_size
                f.write("{},{}\n".format(w, h))
        os.rename(tmp, self.cmd_file)
        return True

    def _send(self, action, arg="", window_size=None):
        """Write a command to the command file and notify swayimg."""
        if not self._running() or self.cmd_file is None:
            return False
        self._write_cmd_file(action, arg, window_size)
        if self.process is not None:
            self.process.send_signal(signal.SIGUSR1)
        return True

    # pylint: disable=too-many-positional-arguments
    def draw(self, path, start_x, start_y, width, height):
        geometry, focus_id, workspace = self._compute_geometry(start_x, start_y, width, height)

        # Cancel any pending hide — we're about to show an image.
        if self._hide_timer is not None:
            self._hide_timer.cancel()
            self._hide_timer = None

        if not self._running():
            self._start(path, geometry)
            self.last_path = path
            self._hidden = False
            return

        win_size = (geometry[2], geometry[3]) if geometry else None

        # Handle window visibility and position via the backend.
        if self._backend is not None and geometry is not None:
            if self._hidden:
                self._backend.show_window(
                    self.APP_ID, geometry[0], geometry[1], workspace)
                self._backend.restore_focus(focus_id)
                self._hidden = False
                self._last_workspace = workspace
            elif geometry != self.last_geometry:
                self._backend.move_window(self.APP_ID, geometry[0], geometry[1])

        # Reload the image in swayimg if something changed.
        if self._hidden or path != self.last_path or geometry != self.last_geometry:
            self._send("show", path, win_size)
            self._last_image_time = time.monotonic()

        # After image update, correct workspace if ranger moved.
        # Throttled: skip if the last image update was less than 1s ago,
        # so that holding down a movement key doesn't trigger repeated IPC.
        if (self._backend is not None and workspace is not None
                and workspace != self._last_workspace
                and time.monotonic() - self._last_image_time > 1.0):
            self._backend.move_to_workspace(self.APP_ID, workspace)
            self._last_workspace = workspace

        self.last_geometry = geometry
        self.last_path = path
        self._hidden = False

    def clear(self, start_x, start_y, width, height):
        if self._hidden:
            return
        # Delay the hide so that a subsequent draw() can cancel it.
        # This prevents close-reopen flicker when switching between images.
        self._hidden = True
        if self._hide_timer is not None:
            self._hide_timer.cancel()
        self._hide_timer = threading.Timer(0.1, self._do_hide)
        self._hide_timer.start()

    def _do_hide(self):
        # Re-check self._hidden: draw() sets it to False before cancelling
        # the timer, so if this fires after a draw() we correctly skip.
        if self._hidden and self._running():
            if self._backend is not None:
                self._backend.hide_window(self.APP_ID)

    def quit(self):
        if self._hide_timer is not None:
            self._hide_timer.cancel()
            self._hide_timer = None
        if self._running() and self.process is not None:
            self._send("exit")
            try:
                self.process.wait(timeout=2)
            except Exception:
                self.process.terminate()
            self.process = None
        if self.cmd_file is not None and os.path.exists(self.cmd_file):
            try:
                os.remove(self.cmd_file)
            except Exception:
                pass
        self.cmd_file = None
