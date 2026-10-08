The Quickshell top bar: a 37px strip on `background` with a 1px `ctosGray` outline, split by 1px `textSecondary` dividers with no gaps.

Order, left to right: SystemLabel (105px `ctosGray` block with the Arch glyph and `CT` in `bar-ct`, then `OS` in `bar-os`), five workspace cells, CPU meter, MEM meter, a flexible spacer, network, user, battery (b1t only, hidden without a battery), status.

- Workspace cells are 37px squares holding a 31px CornerFrame. The active one shows a 9px `ctosGray` crosshair. On b1t a hovered cell shows the crosshair at 0.35 and a click focuses that workspace.
- Every segment is a CornerFrame whose width is its content plus 22px, inside a slot 6px wider.
- Network: a round `success` dot when connected (`textSecondary` when not) framed by 2px accents, then the SSID uppercased, padded with `_` to 7 characters. Wired shows `-WIRED-`, nothing shows `--N/A--`.
- Status reads `ddMMyy-hhmm-TZ-HOST-MICnnnVOLnnn`: date `textSecondary`, `-hhmm-` `textPrimary`, the rest `textSecondary`. On hover MIC and VOL labels turn white and their values green. Click mutes, scroll changes by 1, Shift+scroll by 10.
- Battery: `BAT+` charging, `BAT-` on battery, `BAT=` full on AC, label `textSecondary`, value `textPrimary`, `success` while charging, `error` at 15% or less on battery.

The consumer provides Hyprland workspaces, /proc stats, NetworkManager, UPower and PipeWire. Run it with `CTOS_BAR_ANIMATIONS=reduced` to stop the meter tween.
