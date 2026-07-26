# ranger_swayimg

Display image previews in [ranger](https://github.com/ranger/ranger) using
[swayimg](https://github.com/litoj/swayimg), a Wayland-native image viewer.

A single swayimg instance is kept alive and controlled via a command file +
SIGUSR1, avoiding restart flicker. All compositor-specific window management
(positioning, hiding, focus prevention) is handled by a pluggable backend
layer — no shell `os.execute` calls from the Lua side.

## Requirements

- **swayimg** with the [sai.swi](https://github.com/litoj/sai.swi) Lua plugin
- **Sway** or **Hyprland** compositor
- ranger with `preview_images_method = swayimg` in `rc.conf`

## Installation

1. Place this directory in `~/.config/ranger/plugins/`
2. Install the sai.swi plugin: `~/.config/swayimg/init.lua`
3. Add to `rc.conf`:
   ```
   set preview_images_method swayimg
   ```

## Sway

No additional configuration needed. Everything is handled at runtime via
`swaymsg` IPC:

- Window positioning: `swaymsg '[app_id=...]' move absolute position X Y`
- Window hiding: `swaymsg '[app_id=...]' move to scratchpad`
- Focus prevention: `swaymsg no_focus '[app_id=...]'` (runtime command)
- Focus restoration: `swaymsg '[con_id=...]' focus`

## Hyprland

Most operations work at runtime via `hyprctl`, but **focus prevention requires
a one-time config addition**:

### Required: no_focus window rule

Add this line to your `hyprland.conf`:

```
windowrulev2 = no_focus,class:^ranger-swayimg$
```

Without this, swayimg will steal keyboard focus every time the preview window
appears. The plugin attempts to set this rule at runtime via
`hyprctl keyword`, but this is unreliable across Hyprland versions — the
config entry is the reliable approach.

### How it works (Hyprland)

| Operation         | Command                                                                 |
|-------------------|-------------------------------------------------------------------------|
| Window positioning | `hyprctl dispatch movewindowpixel "dx dy, class:^ranger-swayimg$"`   |
| Window hiding      | `hyprctl dispatch movetoworkspacesilent "special:ranger-preview, ..."` |
| Window showing     | `hyprctl dispatch movetoworkspace "<ws_id>, class:^ranger-swayimg$"`   |
| Focus prevention   | Config-only (`windowrulev2 = no_focus,...`)                            |
| Focus restoration  | Not implemented (skipped)                                               |

The `movewindowpixel` dispatcher is delta-based, so the backend queries the
window's current position from `hyprctl clients -j` and calculates the offset.

Hidden windows are moved to a special workspace named `special:ranger-preview`.

## Architecture

```
ranger_swayimg/
__init__.py            Main displayer class (ranger ImageDisplayer)
ranger_preview.lua     swayimg Lua config (image display + window sizing only)
backends/
    __init__.py        Auto-detection factory
    base.py            Abstract CompositorBackend interface
    sway.py            Sway backend (swaymsg IPC)
    hyprland.py        Hyprland backend (hyprctl IPC)
```

### Backend interface

Each backend implements:

- `detect()` — check if running under this compositor
- `prepare_for_draw()` — return terminal geometry + focused window ID
- `apply_no_focus(app_id)` — prevent focus stealing on launch
- `show_window(app_id, x, y)` — restore hidden window to position
- `hide_window(app_id)` — hide window to scratchpad/special workspace
- `move_window(app_id, x, y)` — reposition visible window
- `restore_focus(window_id)` — restore focus after showing preview

### Command file protocol

Python writes a command file that Lua reads on SIGUSR1:

```
show
/path/to/image
800,600
```

or:

```
exit
```

The Lua side only handles image loading (`sai.viewer.go`) and window sizing
(`sai.set_window_size`). All compositor IPC is in Python.
