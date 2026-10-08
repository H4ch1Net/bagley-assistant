-- Bagley on Hyprland (Lua config).
--
-- Copy to ~/.config/hypr/bagley.lua and add this line to ~/.config/hypr/hyprland.lua:
--   require("bagley")
--
-- The binds talk to the Quickshell instance that loads BagleyOverlay. `qs ipc` has to name that
-- instance the way it was started: the ctOS bar runs as `qs --path /opt/ctos/bar.qml`, so its
-- binds use `-p /opt/ctos/bar.qml`. Change `ipc` below if your bar lives in ~/ctOS or if you run
-- the overlay on its own (desktop/quickshell/bagley/shell.qml).

local ipc = "qs ipc -p /opt/ctos/bar.qml call bagley "
-- Bar in your home folder:
-- local ipc = "qs ipc -p " .. os.getenv("HOME") .. "/ctOS/bar.qml call bagley "
-- Overlay on its own:
-- local ipc = "qs ipc -p " .. os.getenv("HOME") .. "/.config/quickshell/bagley/shell.qml call bagley "

-- SUPER+B: ask Bagley (opens or closes the overlay).
hl.bind("SUPER + B", hl.dsp.exec_cmd(ipc .. "toggle"))

-- SUPER+SHIFT+B: "what's on my screen?" A screenshot of the focused window with its title, the
-- highlighted text and the clipboard go to a vision model; the answer streams into the overlay.
hl.bind("SUPER + SHIFT + B", hl.dsp.exec_cmd(ipc .. "see"))

-- SUPER+ALT+B: explain the highlighted text (or the clipboard).
hl.bind("SUPER + ALT + B", hl.dsp.exec_cmd(ipc .. "explain"))

-- Without Quickshell, the same two answer as mako notifications instead:
-- hl.bind("SUPER + SHIFT + B", hl.dsp.exec_cmd("bagley see"))
-- hl.bind("SUPER + ALT + B", hl.dsp.exec_cmd("bagley explain"))

-- No window rule is needed: the overlay is a layer surface (namespace "bagley"), not a window.
-- Optional: blur behind the translucent panel like rofi, without blurring the clear area around
-- it, and leave the motion to the overlay's own 100ms fade.
-- hl.layer_rule({ match = { namespace = "^bagley$" }, blur = true, ignore_alpha = 0.5, no_anim = true })
