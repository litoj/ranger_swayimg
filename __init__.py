# Display image previews in ranger using swayimg (Wayland image viewer).
#
# Instead of restarting swayimg for every preview, a single instance is kept
# alive and controlled via sai.lib.ipc over a Unix domain socket.  The
# companion swayimg Lua plugin (using sai.swi) lives next to this file in
# ranger_preview.lua.
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
# For Hyprland, add ``windowrulev2 = no_focus,class:^ranger-swayimg-\d+$``
# to ``hyprland.conf`` so the preview window never steals focus.

from __future__ import absolute_import, division, print_function

import os
import socket
import struct
import subprocess
import threading
import time

from ranger.ext.img_display import ImageDisplayer, get_font_dimensions, register_image_displayer

from .backends import detect_backend


@register_image_displayer("swayimg")
class SwayimgImageDisplayer(ImageDisplayer):
    """Image preview using a persistent swayimg instance."""

    APP_ID_PREFIX = "ranger-swayimg"

    def __init__(self):
        self.app_id = "{}-{}".format(self.APP_ID_PREFIX, os.getpid())
        self.process = None
        self.socket_path = None
        self.last_geometry = None
        self.last_path = None
        self._hidden = True
        self._actually_hidden = True
        self._hide_timer = None
        self._last_workspace = None
        self._last_image_time = 0.0
        self._ipc_ready = False
        self._backend = detect_backend()
        self._pending = None
        self._send_cond = threading.Condition()
        self._send_thread = None
        self._worker_stop = False

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
        """Launch swayimg and the companion Lua plugin.

        Note: on the very first preview ranger passes its cache thumbnail
        here; the real image follows a moment later via IPC preview().
        """
        working_dir = self.working_dir or os.environ.get("XDG_RUNTIME_DIR") or "/tmp"
        socket_dir = os.environ.get("XDG_RUNTIME_DIR") or "/tmp"
        self.socket_path = os.path.join(
            socket_dir,
            "ranger-swayimg-{}.sock".format(os.getpid())
        )

        lua_script = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ranger_preview.lua")
        cmd = ["swayimg", "--appid", self.app_id, "--config", lua_script]
        if geometry is not None:
            x, y, w, h = geometry
            cmd.extend(["--position", "{},{}".format(x, y)])
            cmd.extend(["--size", "{},{}".format(w, h)])
        cmd.append(path)

        env = os.environ.copy()
        env["RANGER_SWAYIMG_SOCKET"] = self.socket_path

        # Ensure swayimg never steals focus (blocking — must complete before launch).
        if self._backend is not None:
            self._backend.apply_no_focus(self.app_id)

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
        self._ipc_ready = False
        self._actually_hidden = False
        with self._send_cond:
            self._pending = None
            self._worker_stop = False
        if self._send_thread is None or not self._send_thread.is_alive():
            self._send_thread = threading.Thread(target=self._send_worker, daemon=True)
            self._send_thread.start()

    # IPC design: the main thread never blocks on the socket. _send() stashes
    # only the LATEST request (latest-only coalescing — intermediate images
    # from fast scrolling are irrelevant). A daemon worker sends it; failed
    # sends are dropped — the next draw queues a newer code anyway.
    def _send_worker(self):
        """Daemon thread: send the latest pending code; discard on failure."""
        while True:
            with self._send_cond:
                while self._pending is None and not self._worker_stop:
                    self._send_cond.wait()
                if self._worker_stop:
                    return
                code = self._pending
                self._pending = None
            self._do_send(code)

    def _do_send(self, code):
        """Connect and send one request; drop silently on any failure."""
        proc = self.process
        path = self.socket_path
        if proc is None or proc.poll() is not None or path is None:
            return
        if not self._ipc_ready:
            deadline = time.monotonic() + 5.0
            while not os.path.exists(path):
                if time.monotonic() > deadline or self._worker_stop:
                    return
                time.sleep(0.05)
            self._ipc_ready = True
        code_bytes = code.encode()
        payload = struct.pack("<I", len(code_bytes)) + code_bytes
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(0.5)
        try:
            sock.connect(path)
        except OSError:
            sock.close()
            return
        sock.sendall(payload)
        # shutdown(SHUT_WR) queues a FIN after our data, so the server sees
        # data-then-EOF. WITHOUT the drain below, close() with the server's
        # unread response in our buffer triggers RST instead of FIN, and a
        # busy server would discard the not-yet-read request (this caused
        # silently dropped preview/exit commands). The drain must stay.
        sock.shutdown(socket.SHUT_WR)
        try:
            while sock.recv(4096):
                pass
        except OSError:
            pass
        sock.close()

    def _send(self, code):
        """Stash Lua code for async sending. Only the latest code is kept."""
        with self._send_cond:
            self._pending = code
            self._send_cond.notify()

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
            if self._actually_hidden:
                self._backend.show_window(
                    self.app_id, geometry[0], geometry[1], workspace)
                self._backend.restore_focus(focus_id)
                self._actually_hidden = False
                self._hidden = False
                self._last_workspace = workspace
            elif geometry != self.last_geometry:
                self._backend.move_window(self.app_id, geometry[0], geometry[1])

        # Reload the image in swayimg if something changed.
        if self._hidden or path != self.last_path or geometry != self.last_geometry:
            if win_size is not None:
                code = 'preview({!r}, {}, {})'.format(
                    path, win_size[0], win_size[1])
            else:
                code = 'preview({!r})'.format(path)
            self._send(code)
            self._last_image_time = time.monotonic()

        # After image update, correct workspace if ranger moved.
        # Throttled: skip if the last image update was less than 1s ago,
        # so that holding down a movement key doesn't trigger repeated IPC.
        if (self._backend is not None and workspace is not None
                and workspace != self._last_workspace
                and time.monotonic() - self._last_image_time > 1.0):
            self._backend.move_to_workspace(self.app_id, workspace)
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
                self._backend.hide_window(self.app_id)
            self._actually_hidden = True

    def quit(self):
        if self._hide_timer is not None:
            self._hide_timer.cancel()
            self._hide_timer = None
        # Signal the worker to stop; it observes the flag promptly on its own
        # (idle: immediately, busy: at the next loop check). No join needed —
        # the daemon thread never uses process/socket after this point.
        with self._send_cond:
            self._pending = None
            self._worker_stop = True
            self._send_cond.notify()
        self._send_thread = None
        if self._running() and self.process is not None:
            self.process.terminate()
            self.process = None
        self._ipc_ready = False
        if self.socket_path is not None and os.path.exists(self.socket_path):
            try:
                os.remove(self.socket_path)
            except Exception:
                pass
        self.socket_path = None
