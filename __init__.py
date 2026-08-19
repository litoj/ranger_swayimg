# Display image previews in ranger using swayimg (Wayland image viewer).
#
# Instead of restarting swayimg for every preview, a single instance is kept
# alive and controlled via sai.lib.ipc over a Unix domain socket.  The
# companion swayimg Lua plugin (using sai.swi) lives next to this file in
# ranger_preview.lua.
#
# The preview window stays visible even when ranger loses focus.  It hides
# only once ranger's preview script confirms the selected file is not an
# image (slow thumbnails, e.g. raw/psd, therefore never flicker), or when a
# folder is selected.  All window management is handled by the Python
# backend layer (see the ``backends`` package).
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
from typing import Optional, Tuple, TypeGuard

from ranger.core.shared import FileManagerAware
from ranger.ext.img_display import ImageDisplayer, get_font_dimensions, register_image_displayer

from .backends import detect_backend, DrawContext


class _ViewerProcess(object):
    """Owns the swayimg subprocess and its IPC socket path."""

    def __init__(self, app_id):
        self.app_id = app_id
        self.proc = None
        self.socket_path = None

    @staticmethod
    def _alive(proc: Optional[subprocess.Popen]) -> TypeGuard[subprocess.Popen]:
        """True when *proc* is a live subprocess (TypeGuard for narrowing)."""
        return proc is not None and proc.poll() is None

    def running(self):
        return self._alive(self.proc)

    def start(self, cmd, cwd):
        # pylint: disable=consider-using-with
        try:
            self.proc = subprocess.Popen(
                cmd, cwd=cwd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except OSError:
            self.proc = None
            self.socket_path = None
            return False
        # Matches sai.lib.ipc's default: <runtime>/<app_id>-<swayimg_pid>.socket
        self.socket_path = os.path.join(
            os.environ.get("XDG_RUNTIME_DIR") or "/tmp",
            "{}-{}.socket".format(self.app_id, self.proc.pid))
        return True

    def terminate(self):
        proc = self.proc
        if self._alive(proc):
            proc.terminate()
        self.proc = None
        path = self.socket_path
        if path is not None and os.path.exists(path):
            try:
                os.remove(path)
            except Exception:
                pass
        self.socket_path = None


class _IpcSender(object):
    """Latest-only async sender for the swayimg IPC socket.

    The main thread never blocks on the socket: send() stashes only the
    LATEST request (intermediate images from fast scrolling are skipped)
    and a daemon worker delivers it.  Failed sends are dropped — the next
    draw queues a newer request anyway.
    """

    def __init__(self, viewer):
        self._viewer = viewer
        self._pending = None
        self._cond = threading.Condition()
        self._thread = None
        self._stop = False
        self._ready = False

    def send(self, code):
        with self._cond:
            self._pending = code
            self._cond.notify()

    def start(self):
        with self._cond:
            self._pending = None
            self._stop = False
            self._ready = False
            if self._thread is None or not self._thread.is_alive():
                self._thread = threading.Thread(target=self._worker, daemon=True)
                self._thread.start()

    def stop(self):
        """Signal the worker to stop; it observes the flag promptly on its
        own (idle: immediately, busy: at the next loop check).  No join
        needed — the daemon worker never touches viewer/socket again."""
        with self._cond:
            self._pending = None
            self._stop = True
            self._cond.notify()
        self._thread = None

    def _worker(self):
        while True:
            with self._cond:
                while self._pending is None and not self._stop:
                    self._cond.wait()
                if self._stop:
                    return
                code = self._pending
                self._pending = None
            self._do_send(code)

    def _do_send(self, code):
        viewer = self._viewer
        path = viewer.socket_path
        if not viewer.running() or path is None:
            return
        if not self._ready:
            deadline = time.monotonic() + 5.0
            while not os.path.exists(path):
                if time.monotonic() > deadline or self._stop:
                    return
                time.sleep(0.05)
            self._ready = True
        code_bytes = code.encode()
        payload = struct.pack("<I", len(code_bytes)) + code_bytes
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(0.5)
        try:
            sock.connect(path)
            sock.sendall(payload)
            # shutdown(SHUT_WR) queues a FIN after our data, so the server
            # sees data-then-EOF.  WITHOUT the drain below, close() with
            # the server's unread response in our buffer triggers RST
            # instead of FIN, and a busy server would discard the
            # not-yet-read request (this caused silently dropped preview/exit
            # commands).  The drain must stay.
            sock.shutdown(socket.SHUT_WR)
            while sock.recv(4096):
                pass
        except OSError:
            pass
        finally:
            sock.close()


# Visibility is a 3-state machine: SHOWN -> HIDE_PENDING -> HIDDEN.  Every
# hide — for a folder, a decided non-image, or a script verdict — passes
# through HIDE_PENDING and fires only after a short grace timer; a draw()
# in the same breath cancels it.  That absorbs the clear()+draw() volley
# ranger emits when the previewed file is deleted (its previews entry is
# already gone, so verdict-based deferral cannot apply), and it funnels
# every compositor hide through one spot, where it can be vetoed while the
# user is on another workspace.
class _VisibilityState(object):
    """Preview window visibility state machine."""

    SHOWN = "shown"
    HIDE_PENDING = "hide_pending"
    HIDDEN = "hidden"

    HIDE_GRACE = 0.3

    def __init__(self):
        self.state = self.HIDDEN
        self._timer = None

    def begin_draw(self):
        """Commit SHOWN, cancel a pending hide, and return the prior state."""
        prior = self.state
        self.state = self.SHOWN
        self._cancel_timer()
        return prior

    def defer_hide(self):
        """Move to HIDE_PENDING (waiting for the preview-script verdict)."""
        self._cancel_timer()
        if self.state == self.SHOWN:
            self.state = self.HIDE_PENDING

    def schedule_hide(self, op):
        """Arm the hide grace timer; *op* fires afterwards unless cancelled.

        *op* must return True when the window is now hidden; returning False
        (e.g. the user is on another workspace) leaves HIDE_PENDING in
        place so a later event can retry.
        """
        self._cancel_timer()
        if self.state == self.HIDDEN:
            return
        self.state = self.HIDE_PENDING

        def expire():
            if self.state != self.HIDE_PENDING:
                return
            if op():
                self.state = self.HIDDEN
        self._timer = threading.Timer(self.HIDE_GRACE, expire)
        self._timer.daemon = True
        self._timer.start()

    def hide(self):
        self._cancel_timer()
        self.state = self.HIDDEN

    def _cancel_timer(self):
        if self._timer is not None:
            self._timer.cancel()
            self._timer = None


class _DrawState(object):
    """Bookkeeping for the last draw: what was shown, where, and when."""

    def __init__(self):
        self.path = None  # type: Optional[str]
        self.geometry = None  # type: Optional[tuple]
        self.workspace = None  # type: Optional[object]
        self.image_time = 0.0


@register_image_displayer("swayimg")
class SwayimgImageDisplayer(ImageDisplayer, FileManagerAware):
    """Image preview using a persistent swayimg instance."""

    APP_ID_PREFIX = "ranger-swayimg"

    def __init__(self):
        self.app_id = "{}-{}".format(self.APP_ID_PREFIX, os.getpid())
        self._backend = detect_backend()
        self._viewer = _ViewerProcess(self.app_id)
        self._ipc = _IpcSender(self._viewer)
        self._visibility = _VisibilityState()
        self._hide_gen = 0
        self._last = _DrawState()
        # Ranger only clears its previews dict when preview_images is toggled
        # off — it never calls clear()/quit() on the displayer, and the browser
        # column doesn't redraw (need_redraw stays False for the same file).
        # Without this hook the swayimg window would stay visible forever.
        self._preview_sig = self.fm.settings.signal_bind(
            'setopt.preview_images', self._on_preview_images_changed,
            priority=0.3)

    def _on_preview_images_changed(self, signal):
        if not signal.value:
            self._hide_gen += 1
            self._visibility.schedule_hide(self._do_hide)

    # pylint: disable=too-many-positional-arguments
    def draw(self, path, start_x, start_y, width, height):
        geometry, ctx = self._compute_geometry(start_x, start_y, width, height)

        # While the terminal is fullscreen, leave swayimg completely alone:
        # no launch, no IPC, no window moves.  Everything resumes on the
        # first draw after fullscreen is exited.
        if ctx.fullscreen:
            return

        self._hide_gen += 1
        prev_state = self._visibility.begin_draw()

        # While the user is on another workspace (e.g. ranger's folder was
        # updated in the background), compositor operations that steal the
        # view are forbidden: scratchpad-show and focus would rip the user
        # out of fullscreen or another workspace.  Skip the draw entirely
        # and stay HIDDEN so the next draw retries once they're back.
        away = ctx.ws_current is False

        if not self._viewer.running():
            if away:
                self._visibility.hide()
                return
            self._start(path, geometry, ctx.workspace)
            self._last.path = path
            return

        win_size = (geometry[2], geometry[3]) if geometry else None

        if self._backend is not None and geometry is not None:
            if prev_state == _VisibilityState.HIDDEN:
                if away:
                    self._visibility.hide()
                    return
                self._backend.show_window(
                    self.app_id, geometry[0], geometry[1], ctx.workspace)
                self._backend.restore_focus(ctx.focus_id)
                self._last.workspace = ctx.workspace
            elif geometry != self._last.geometry:
                self._backend.move_window(self.app_id, geometry[0], geometry[1])

        if (prev_state != _VisibilityState.SHOWN
                or path != self._last.path or geometry != self._last.geometry):
            args = "{!r}".format(path)
            if win_size is not None:
                args += ", {}, {}".format(*win_size)
            self._ipc.send("preview({})".format(args))
            self._last.image_time = time.monotonic()

        # After image update, correct workspace if ranger moved.
        # Throttled: skip if the last image update was less than 1s ago,
        # so that holding down a movement key doesn't trigger repeated IPC.
        if (self._backend is not None and ctx.workspace is not None
                and ctx.workspace != self._last.workspace
                and time.monotonic() - self._last.image_time > 1.0):
            self._backend.move_to_workspace(self.app_id, ctx.workspace)
            self._last.workspace = ctx.workspace

        self._last.geometry = geometry
        self._last.path = path

    def _compute_geometry(
        self, start_x: int, start_y: int, width: int, height: int
    ) -> Tuple[Optional[Tuple[int, int, int, int]], DrawContext]:
        """Compute preview pixel geometry and capture compositor state.

        Returns (preview_geometry, context) where preview_geometry is the
        pixel (x, y, w, h) for the preview window, or None when it cannot be
        computed, and context is the DrawContext snapshot whose ``fullscreen``
        and ``ws_current`` fields gate everything draw() may do to windows.
        """
        fallback = DrawContext(None, None, None, False, None)
        if self._backend is None:
            return None, fallback

        try:
            font_width, font_height = get_font_dimensions()
        except Exception:
            return None, fallback

        ctx = self._backend.prepare_for_draw()
        if ctx.term_geometry is None:
            return None, ctx

        term_x, term_y = ctx.term_geometry[0], ctx.term_geometry[1]

        preview_x = term_x + start_x * font_width
        preview_y = term_y + start_y * font_height
        preview_w = width * font_width
        preview_h = height * font_height

        preview_x = max(0, preview_x)
        preview_y = max(0, preview_y)

        return (preview_x, preview_y, preview_w, preview_h), ctx

    def _start(self, path, geometry, workspace):
        """Launch swayimg and the companion Lua plugin, then place the window.

        Note: on the very first preview ranger passes its cache thumbnail
        here; the real image follows a moment later via IPC preview().
        """
        lua_script = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ranger_preview.lua")
        cmd = ["swayimg", "--appid", self.app_id, "--config", lua_script]
        if geometry is not None:
            x, y, w, h = geometry
            cmd.extend(["--position", "{},{}".format(x, y)])
            cmd.extend(["--size", "{},{}".format(w, h)])
        cmd.append(path)

        # Ensure swayimg never steals focus (blocking — must complete before launch).
        if self._backend is not None:
            self._backend.apply_no_focus(self.app_id)

        self._viewer.start(cmd, self.working_dir)
        proc = self._viewer.proc
        # Place the window explicitly once it maps, so the initial render
        # lands on the terminal's workspace instead of wherever the
        # compositor happened to put it.  wait_mapped() with alive= bails
        # out early if swayimg dies before mapping, instead of freezing the
        # UI for the full timeout on every retry.
        if (proc is not None and geometry is not None and self._backend is not None
                and self._backend.wait_mapped(proc.pid, alive=self._viewer.running)):
            self._backend.show_window(
                self.app_id, geometry[0], geometry[1], workspace)
        self._last.geometry = geometry
        self._last.workspace = workspace
        self._last.image_time = 0.0
        self._ipc.start()

    def clear(self, start_x, start_y, width, height):
        # ranger calls clear() the moment the cursor moves, before using the
        # new file's preview: while the preview script is still loading (raw/
        # psd thumbnails take time) the verdict is pending, and is_image()
        # means draw() follows in the same UI cycle.  In both cases the
        # window must stay shown, else every image switch flickers through a
        # hide.  The decided hide also waits out a grace period: after the
        # previewed file itself is deleted, ranger clears for it (its
        # previews entry is already gone) right before drawing the next one.
        self._hide_gen += 1
        thisfile = self.fm.thisfile
        path = (thisfile.realpath
                if thisfile is not None and thisfile.is_file else None)
        data = self.fm.previews.get(path, {}) if path else {}
        undecided = bool(data.get('loading'))
        image = 'imagepreview' in data or 'directimagepreview' in data
        if not undecided and not image:
            self._visibility.schedule_hide(self._do_hide)
            return
        self._visibility.defer_hide()
        if undecided:
            threading.Thread(
                target=self._watch_decision,
                args=(path, self._hide_gen),
                daemon=True).start()

    def _watch_decision(self, path, gen):
        """Hide the window once the preview script denies an image preview.

        Runs on a short-lived daemon thread; *gen* vetoes the hide when a
        newer clear/draw superseded this one before the verdict arrived.
        """
        deadline = time.monotonic() + 30.0
        while time.monotonic() < deadline:
            if gen != self._hide_gen:
                return
            data = self.fm.previews.get(path)
            if data is None or not data.get('loading'):
                if data is not None and (
                        'imagepreview' in data or 'directimagepreview' in data):
                    return
                break
            time.sleep(0.05)
        if gen != self._hide_gen:
            return
        self._visibility.schedule_hide(self._do_hide)

    def _do_hide(self):
        """Fire the compositor hide; False when it must wait (see below).

        Called at the end of the hide grace period; only then is the state
        marked HIDDEN.  Refuses to fire while the user is on another
        workspace: sway's scratchpad move would rip them out of their
        fullscreen view — the drop keeps state and window consistent, and
        the next displayer event after they return normalizes everything.
        """
        if self._backend is None or not self._viewer.running():
            return True
        if self._backend.prepare_for_draw().ws_current is False:
            return False
        self._backend.hide_window(self.app_id)
        return True

    def quit(self):
        sig = self._preview_sig
        if sig is not None:
            self._preview_sig = None
            try:
                self.fm.settings.signal_unbind(sig)
            except ValueError:
                pass
        self._hide_gen += 1
        self._visibility.hide()
        self._ipc.stop()
        self._viewer.terminate()
