# ranger_swayimg

Display image previews in [ranger](https://github.com/ranger/ranger) using
[swayimg](https://github.com/litoj/swayimg), a Wayland-native image viewer.

A single swayimg instance is kept alive and controlled via `sai.lib.ipc` over a Unix domain socket,
avoiding restart flicker. All compositor-specific window management (positioning, hiding, focus
prevention) is handled by a pluggable backend layer — no shell `os.execute` calls from the Lua side.

## Requirements

- **swayimg** with the [sai.swi](https://github.com/litoj/sai.swi) Lua plugin
- **Sway** or **Hyprland** compositor

## Installation

1. Place this directory in `~/.config/ranger/plugins/`
2. Add to `rc.conf`: `set preview_images_method swayimg`

### Sway

No additional configuration needed. Everything is handled at runtime via `swaymsg` IPC:

- Window positioning: `swaymsg '[app_id=...]' move absolute position X Y`
- Window hiding: `swaymsg '[app_id=...]' move to scratchpad`
- Focus prevention: `swaymsg no_focus '[app_id=...]'` (runtime command)
- Focus restoration: `swaymsg '[con_id=...]' focus`

### Hyprland

Most operations work at runtime via `hyprctl`, but **focus prevention requires a one-time config
addition**:

#### Required: no_focus window rule

Add this line to your `hyprland.conf`:

```
windowrulev2 = no_focus,class:^ranger-swayimg-\d+$
```

Without this, swayimg will steal keyboard focus every time the preview window appears. The plugin
attempts to set this rule at runtime via `hyprctl keyword`, but this is unreliable across Hyprland
versions — the config entry is the reliable approach.

#### How it works

| Operation          | Command                                                                  |
| ------------------ | ------------------------------------------------------------------------ |
| Window positioning | `hyprctl dispatch movewindowpixel "dx dy, class:^ranger-swayimg-\d+$"`   |
| Window hiding      | `hyprctl dispatch movetoworkspacesilent "special:ranger-preview, ..."`   |
| Window showing     | `hyprctl dispatch movetoworkspace "<ws_id>, class:^ranger-swayimg-\d+$"` |
| Focus prevention   | Config-only (`windowrulev2 = no_focus,...`)                              |
| Focus restoration  | Not implemented (skipped)                                                |

The `movewindowpixel` dispatcher is delta-based, so the backend queries the window's current
position from `hyprctl clients -j` and calculates the offset.

Hidden windows are moved to a special workspace named `special:ranger-preview`.

### IPC protocol

Python connects to a Unix domain socket exported by the Lua side via `sai.lib.ipc` and sends
length-prefixed Lua code. The Lua config starts an IPC server allowing for lua execution and
provides a `preview(path, w, h)` method.
