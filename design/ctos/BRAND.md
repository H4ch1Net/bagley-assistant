ctOS is the Watch Dogs-style desktop on b1t: TSM-061/ctOS (a Quickshell bar and greeter) on Hyprland, with kitty, rofi, mako, fuzzel, Powerlevel10k, fastfetch and Zed themed to match. Every value here comes from the upstream source or from b1t's own configs. Where b1t differs from upstream, the b1t value is used and the difference is noted.

## Content fundamentals

- UI text is uppercase, machine-like and terse. Use the source's own strings verbatim: `PASSPHRASE`, `LOGIN`, `INITIALIZING...`, `CPU <>`, `MEM ##`, `-WIRED-`, `--N/A--`, `BAT+` (charging), `BAT-` (discharging), `BAT=` (on AC), `MIC`, `VOL`.
- Numbers are fixed-width and zero-padded: CPU `012%` (3 digits), MEM `3.4G`, MIC/VOL `045`, date `ddMMyy` (`071026`), time `-hhmm-` (`-1643-`), then `<TZ>-<HOST>-` (`PDT-B1T-`). Network names are uppercased, padded to 7 with `_` and cut to the last 7 characters.
- Log lines read like a system talking to itself: `REGION_LINK_ESTABLISHED : B1T`, `[BLUME_IDP] Authentication Session opened.`, `[BLUME_IDP] IDENTITY_VERIFIED // SID:<24 hex>`, `[SENTINEL] Authentication Failed (TraceId: <16 hex>)`. Hex is uppercase. The prompt prefix is `» `.
- Lore names stay as written: Blume Corp, Sentinel, `blume-krn-1.0.8 <> ctOS-1.0.0-a`. The disclaimer is exactly two lines: `Property of Blume Corp. All usage is` / `subject to Sentinel Active Monitoring.`
- Shell text is plain: the banner says `● ONLINE`, `UPTIME`, `TIP`. Key names in tips are uppercase (`SUPER+R  launcher`).
- No emoji. State is carried by a word (`ONLINE`, `BAT-015`, `CRITICAL`) and the color only reinforces it.

## Color

- The ground is `background` (#0E0E0E). Raised surfaces are `backgroundBright` (#202020). Translucent panels (rofi, mako, fuzzel) use `panel-translucent`.
- Chrome is `ctosGray` (#D9D9D9): the 1px bar outline, the SystemLabel block, the lockup bar, the LOGIN tab, the field border, the active workspace crosshair and the start of the window border gradient.
- Corner brackets are `textPrimary` white, never gray.
- Text tiers: `textPrimary` for values, `textPrimaryDim` for body, `textPrimaryDimmer` for the clock and logs, `textSecondary` for labels and dividers. `textSecondary` reads at 4.5:1 on `background` and only 3.8:1 on `backgroundBright`, so keep muted text on `background`.
- `success` (#00FA9A on b1t, #1bfd9c upstream) and `error` (#fc3e38) are the only hues in the UI. Green means online, verified, charging, clean, exit 0. Red means failed, conflict, critical, low battery. Neither is ever decoration.
- Terminals use the Mono Glow palette (`term-*`, `ansi0` to `ansi15`): grays plus `ansi2` green, `ansi10` mint for strings, and two muted teals (`ansi5`, `ansi13`).

## Type

- One family: JetBrainsMono Nerd Font (`mono`). kitty and Zed buffers use the Mono variant. mako and fuzzel use plain JetBrains Mono.
- The lockup pairs a thin, large `OS` with a medium, smaller `CT`: `bar-os` 36/300 with `bar-ct` 22/500 in the bar, `splash-os` 64/300 with `splash-ct` 34/500 on the lock screen.
- The clock is the thinnest weight in the system: `clock` 48/100.
- Bar text is 16/500 (`bar-segment`); meters are 12/600 (`bar-meter`).
- Greeter sizes are written for 1080p and scale with screen height (`vh = height / 1080`). Bar sizes are fixed pixels.

## Shape, space and depth

- Square everything (`radius-none`). The only round shape in the whole system is the network status dot (`radius-dot`).
- The bar is `bar-height` 37px, outlined in `border-hairline` `ctosGray`, split by 1px `textSecondary` dividers with no gaps.
- Every bar segment sits in a CornerFrame: four white L-corners with `corner-arm` 7px arms, 1px thick, `corner-margin` 4px from the content, `bar-height` minus `frame-inset` tall (31px).
- Accents are 4px quarter discs (`accent.svg`, white at 0.95) placed `accent-offset-x` 18px and `accent-offset-y` 10px outside an element's corners. They frame the lockup, the clock icon and the version box.
- Windows: `gaps-in` 5px, `gaps-out` 12px, `border-window` 2px with a `ctosGray` to `textSecondary` gradient at 45 degrees when active and `backgroundBright` when inactive. Blur size 6, 2 passes. `window-shadow` is the only shadow.
- Terminals: `terminal-opacity` 0.85 with `kitty-blur` 32 over the wallpaper, `kitty-padding` 14px, inactive text at 0.6.

## Motion

- The bar barely moves. The meter fill tweens over `meter-tween` 1000ms with `ease-in-out-sine`, and b1t turns that off (`CTOS_BAR_ANIMATIONS=reduced`).
- Accents fade in over 50ms and slide out over 100ms with `ease-in-cubic`.
- The lock screen splash: pause 500ms, fill to 40% over 700ms, to 100% over 300ms (OutSine), expand vertically over 350ms (OutCubic), retract to 73% over 300ms (OutQuart), then the layout slides apart over 500ms (`ease-in-out-circ`). On b1t the lock runs in reduced mode, which skips the splash and shows everything in its final state.
- Small loops: the spinner steps every 100ms around a 3 by 3 grid of 5px squares; text dots cycle 1 to 3 over 900ms; the cursor `▁` holds 500ms, fades 300ms, stays off 300ms; the typewriter writes 10ms per character.
- After a correct password: the field collapses to a 4px bar, fills to 40% over 1000ms, pauses 300ms, then to 100% over 300ms while `INITIALIZING...` counts up.
- The live wallpaper is a slow 24 second loop of wallpaper-v1 with no flashing.

## Imagery and motifs

- Backgrounds are dithered near-black (#0a to #13) with a faint square grid: minor lines every 20px, major every 160px (`lock.png`).
- The ctOS diamond (`os-icon.svg`): a square rotated 45 degrees with a center line and a filled inner diamond. wallpaper-v1 puts it at 660px in the center; wallpaper-v2 puts it at 160px in the bottom-left.
- The right edge of both wallpapers carries a vertical ID strip: `barcode` blocks and the text `SYD-AU-NSW-02|8D5A6D11-7C25-4442-BCAA-81749930DD9F`.
- Blume marks: the tesseract (wireframe octahedron) and the hex-cube logo appear on the lock screen only.

## Iconography

- The bar uses text glyphs: `▨` for the user, `<>` and `##` as meter symbols, a 9px crosshair for the active workspace, and an Arch glyph (`distro-arch.svg`) in the SystemLabel block.
- App icons (`ctos-*.svg`, 64px): a `background` tile with a 1px `textSecondary` outline, 12px `ctosGray` corner brackets at 2px, a `ctosGray` line glyph at 2.5px with miter joins, secondary strokes in `textSecondary`, and at most one `success` pixel.
- Nerd Font glyphs (the distro icon, git branch) appear in the shell prompt only.

## Bagley

- Bagley's avatar is a tracked node graph: a ctOS diamond hub (ID00) and square satellite nodes in a square viewport, edges carrying signal packets, a tracking box and ID on every node. In ctOS colors it draws in `ctosGray` and white, turns `success` on DONE and `error` on ERROR, and blinks white on AWAIT.
