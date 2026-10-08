rofi with `ctos.rasi`, opened with Super+R: a centered 560px window on `panel-translucent` with a 1px `ctosGray` border, JetBrainsMono Nerd Font 12.

- Main box: 14px padding, 12px spacing.
- Input bar: 8px by 10px padding, a 1px `textSecondary` line underneath, the prompt `ctOS` in `success`, the placeholder `SEARCH` in `textSecondary`, typed text `textPrimary`.
- List: 8 lines, one column, no scrollbar, 2px between rows. Rows have 8px by 10px padding, 12px between the 26px icon and the label, `textPrimaryDim` text and a transparent 2px left border.
- Selected row: `backgroundBright` fill, `ctosGray` left border, `textPrimary` text.
- The clipboard picker (Super+Shift+V) is the same theme at 700px, no icons, prompt `clip`. fuzzel is the fallback with the same colors and `> ` as its prompt.

Terminal apps launch as `kitty --class ctos-term -e ...`. Entries that duplicate a ctOS one (Dolphin, VS Code, Spotify, btop, nvim) are hidden.
