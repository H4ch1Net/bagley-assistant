# ctOS design spec (source-backed)

Paths: `UP/` = the upstream TSM-061/ctOS repo. `USR/` = b1t's home directory (/home/h4ch1). `OUT/` = setup files made during the b1t ctOS setup (not in this repo).
Every value below is copied from the named file. "computed" means derived from a source expression (formula given). "not in source" means the source does not set it.

Scale unit: `Units.vh = Screen.height / 1080` (UP/common/Units.qml). Greeter sizes written `N*vh` scale with screen height; bar sizes are fixed px.

---

## 1. Palette

### 1.1 Theme.qml primitives (UP/common/Theme.qml)
| token | value | notes |
|---|---|---|
| gray50 | `#ffffff` | |
| gray100 | `#CACACA` | |
| gray200 | `#D9D9D9` | the "ctOS gray" |
| gray300 | `#c3c3c3` | |
| gray500 | `#7a7a7a` | |
| gray700 | `#202020` | |
| gray800 | `#0E0E0E` | |
| accentGreen | `#1bfd9c` upstream; **`#00FA9A` in USR/ctOS/common/Theme.qml** (user override) | |
| accentRed | `#fc3e38` | |

(No gray400/gray600 exist.)

### 1.2 Theme.qml semantic roles
| role | maps to | hex |
|---|---|---|
| background | gray800 | #0E0E0E |
| backgroundBright | gray700 | #202020 |
| ctosGray | gray200 | #D9D9D9 |
| textPrimary | gray50 | #ffffff |
| textPrimaryDim | gray100 | #CACACA |
| textPrimaryDimmer | gray300 | #c3c3c3 |
| textSecondary | gray500 | #7a7a7a |
| secondary | gray500 | #7a7a7a |
| textAccent | accentGreen | #1bfd9c (user #00FA9A) |
| success | accentGreen | #1bfd9c (user #00FA9A) |
| error | accentRed | #fc3e38 |
| fontFamily | `"JetBrainsMono Nerd Font"` | |

### 1.3 Literal colors outside Theme (QML)
| value | file | use |
|---|---|---|
| `"white"` | bar/components/CornerFrame.qml `accentColor` default | all bar corner brackets (no caller overrides it) |
| `"white"` | bar/components/Meter.qml | the 3 meter label Texts (name, symbol, value) |
| `"transparent"` | bar.qml | 1px outer border rectangle fill |
| `'#1effffff'` (white, alpha 0x1e = 30/255 ≈ 11.8%) | greeter/components/IdentityCard.qml | profile-picture backdrop (component unused) |
| `'#14ffffff'` (white, alpha 0x14 = 20/255 ≈ 7.8%) | greeter/components/Surface.qml | Surface center panel fill |
| `#414141` | greeter/components/Time.qml | 1px border of date pill |
| `#B1B1B1` | greeter/components/Time.qml | date pill text |
| `"#ff0e0e0e"` | greeter/views/Locker.qml | lock surface base rect |
| `Qt.darker(Theme.textPrimaryDimmer, 2)` | greeter/components/Status.qml | Surface side-bar color; computed ≈ #616161 (QColor value 195/2) |
| `Qt.darker(Theme.textPrimary, 1.6)` | greeter/components/Terminal.qml | version box border; computed ≈ #9f9f9f |
| `Qt.darker(Theme.textPrimary, 1.2)` | greeter/components/Terminal.qml | version text; computed ≈ #d4d4d4 |

### 1.4 Opacities used in QML
- accent.svg fill-opacity `0.95`; tesseract.svg back face `opacity:0.612`.
- IdentityCard user.svg `opacity: 0.9`.
- Splash exit: progress rect opacity → `0.1`.
- Workspace inactive crosshair opacity `0`; **user override: `0.35` on hover** (USR/ctOS/bar/components/Workspace.qml).
- All reveal animations go 0 → 1.

### 1.5 SVG literal colors
| color | where |
|---|---|
| `#d9d9d9` stroke | bar/resources/corner-frame.svg, corner-square.svg |
| `#0E0E0E` fill/stroke | bar/resources/distro-arch.svg (drawn on the #D9D9D9 SystemLabel block) |
| `#ffffff` fill-opacity 0.95 | greeter/resources/accent.svg |
| `#0e0e0e` fill | greeter/resources/arrow.svg |
| `#ffffff` | barcode.svg fill; tesseract.svg (front face opaque, back face opacity 0.612); os-icon.svg `fill="white"` |
| `#0e0e0e` stroke | blume-logo.svg |
| `#B6B6B6` | device-barcode.svg; id-barcode.svg card squares |
| `#CACACA` | device-text.svg glyphs |
| `#0E0E0E`, `#D9D9D9`, `#E6E6E6`, `white`, `#B6B6B6` | id-barcode.svg |
| `#0E0E0E` | user-barcode.svg bars |
| `#cdcdcd` | user.svg silhouette (a hidden `#f61f03` layer exists, `display:none`) |
| Inkscape page colors `#505050`/`#eeeeee` | editor metadata only, not rendered |

### 1.6 Raster image colors (measured from pixels)
- lock.png, wallpaper-v1/v2, Logo.png background: dithered near-black, most common pixels `#0f0f0f`, `#0e0e0e`, `#0d0d0d`, `#101010`, `#0c0c0c`, `#111111` (range ≈ #0a0a0a–#131313).
- Light shapes in wallpapers/logo: `#d9d9d9`. Wallpaper side strip: `#b6b6b6`. Logo "OS": `#ffffff`.

### 1.7 User palette extensions (configs)
| value | where | role |
|---|---|---|
| `#00FA9A` | Theme.qml, kitty color2, active_border_color, mark1, rofi `ok`, p10k ok/clean, zed success/created/string.escape, ctos-banner `ok` (`38;2;0;250;154`), fastfetch host | success / online / ok |
| `#fc3e38` / `#FC3E38` | kitty bell_border_color + mark3, mako critical border, p10k errors, zed error/deleted/conflict | error |
| `#0E0E0Eee` | rofi bg, mako background, fuzzel `0E0E0Eee` | translucent panel (alpha 0xee ≈ 93%) |
| `#202020` | rofi panel/selected bg, mako low-urgency border, fuzzel selection, Hyprland inactive border, p10k gap char, zed element bg | raised surface |
| `#CACACA` | rofi fg, mako text, fuzzel text, p10k dir, banner `g` (202,202,202) | body text |
| `#7A7A7A` | rofi dim, fuzzel placeholder, p10k secondary segments, banner `d`, Hyprland border gradient end | muted |
| `#D9D9D9` | rofi line/border, mako border, fuzzel prompt/border, Hyprland border gradient start, p10k os_icon, slurp `-c d9d9d9ff` | frame lines |
| `#FFFFFF` / `#f1f1f1` | rofi entry/selected text; fuzzel input `f1f1f1`; fastfetch keys `#f1f1f1` & logo `38;2;241;241;241` | emphasis |
| `0e0e0eaa` | ctos-shot slurp background (`-b`) | selection dim |
| zed extras | `#121212` surfaces, `#2a2a2a`/`#3a3a3a` hover/active, `#555555` disabled, `#4a4a4a` line numbers, `#333333` invisibles, `#ffffff0d/18/22/28/2e/33/44` overlays, `#a6ffc9` strings, `#dddddd` numbers, `#aaaaaa` attributes/info, `#708090` link_uri, `#fc3e3822`, `#00fa9a1a`, `#fc3e381a`, `#ffffff10/14` | USR/.config/zed/themes/ctos.json |

Superseded (older, Oct 6): `OUT/ctos/kitty.conf.vibrant` used fg `#00FA9A` on `#1a1a1a` with saturated ANSI (`#FF3355`, `#FFE81F`, `#4A85FF`, `#E03DFF`, `#00E5FF` …); `OUT/ctos/ctOS.colors` is a KDE scheme (bg 14,14,14 / 18,18,18, accent 0,250,154, negative 252,62,56, neutral 217,184,74, visited 102,178,178). Not used by current Hyprland setup.

---

## 2. Typography

Family: `JetBrainsMono Nerd Font` (Theme.fontFamily; greeter `Settings.fontFamily` default from GeneralDto `"JetBrainsMono Nerd Font"`, configurable). Kitty/zed buffer/terminal: `JetBrainsMono Nerd Font Mono`. Mako/fuzzel: `JetBrains Mono`. Rofi: `JetBrainsMono Nerd Font 12`. Splash/README legal: none other. Weights are numeric (Qt CSS-like).

### 2.1 Bar (px)
| element | size | weight | color | file |
|---|---|---|---|---|
| SystemLabel "CT" (inside gray block) | 22 | 500 | Theme.background | SystemLabel.qml |
| SystemLabel "OS" | 36 | 300 | Theme.ctosGray | SystemLabel.qml |
| Meter name / symbol / value | 12 | 600 | "white" | Meter.qml |
| SimpleSegment text (net SSID, USER) | 16 | 500 | textPrimary | SimpleSegment.qml |
| user glyph `▨` | 18 | 600 | textPrimary | bar.qml |
| Status (date/time/tz/MIC/VOL) | 16 | 500 | mixed | Status.qml |
| Battery (user) | 16 | 500 | mixed | patch Battery.qml |

### 2.2 Greeter (px)
| element | size | weight | color | file |
|---|---|---|---|---|
| Splash "OS" | 64 (`fontSizeMode: Text.Fit`) | 300 | textPrimary | Splash.qml |
| Splash "CT" | 34 (Fit) | 500 | background | Splash.qml |
| Clock "hh:mm" | 48 | 100 | textPrimaryDimmer | Time.qml |
| Date pill "dddd dd MMMM" | 14 | 300 | #B1B1B1 | Time.qml |
| "PASSPHRASE" label (Typewriter) | 14 | not set | textPrimary | FieldGroup.qml |
| Password field | 16, `letterSpacing: 5` | not set | state color | PasswordField.qml (family **not set** on the field) |
| "LOGIN" | 16 | not set | background | FieldGroup.qml |
| progress % ("00".."100") | 14 | 500 | textPrimaryDim | FieldGroup.qml |
| "INITIALIZING…" | 14 | not set | textPrimaryDimmer | FieldGroup.qml |
| Disclaimer lines | 11 (Fit, min 1) | not set | ctosGray | Disclaimer.qml |
| Session username (Typewriter) | 14 | not set | textPrimary | Session.qml |
| Terminal log / prompt | `14 * vh` | not set | Dimmer / Dim | Terminal.qml |
| Terminal version | `13 * vh` | not set | darker(white,1.2) | Terminal.qml |
| Status InfoField label / value | 12 / 18 | – / 500 | Dim / Primary | Status.qml (unused) |
| IdentityCard label / value | 14 / 22 | – / 500 | Dim / Primary | IdentityCard.qml (unused) |
| ThinProgress % | 12 | 300 | textPrimary | ThinProgress.qml (unused) |

### 2.3 Capitalization and string formats (verbatim)
- Bar: `"CT"`, `"OS"`, `"CPU"` + `"<>"`, `"MEM"` + `"##"`, CPU value `NNN%` (padStart 3 "0", e.g. `000%`), MEM value `N.NG` (`toFixed(1)+"G"`), net `"-WIRED-"` / `"--N/A--"` / SSID uppercased with `[-_ ]?[25](.4)?g` removed, delimiters `-_ ` removed, `padEnd(7,"_")`, last 7 chars; user `$USER.toUpperCase()`; date `ddMMyy`; time `-hhmm-`; placeholder `"AEDT-1234-"` (user patch: `` `${tz abbrev}-${HOSTNAME}-` ``); `"MIC"`/`"VOL"` + 3-digit (`000` when muted); user Battery `"BAT+"` charging / `"BAT-"` discharging / `"BAT="` on AC, + 3-digit level.
- Greeter: `"PASSPHRASE"`, `"LOGIN"`, `"INITIALIZING"` + 1–3 `.`, `"OS"`, `"CT"`, `"Property of Blume Corp. All usage is"`, `"subject to Sentinel Active Monitoring."`, `"blume-krn-1.0.8 <> ctOS-1.0.0-a"`, labels `"EMPID ##"`, `"CLASS"`, `"FULL NAME"`, `"BATTERY"`/`"ENV"`, `"NODE"`.
- Terminal boot lines (TerminalManager.qml): `REGION_LINK_ESTABLISHED : AU-SOUTH-EAST-2` (user: hostname uppercased), `LOG_STREAM_CONNECTED // 1B7C5296-469D-4595-AD5D-4E31349CF13F`, `WL_OUTPUT_FOUND: <monitor> <-> ADDR_PTR: 0x<8 hex>`, `---GREETER_UI_INITIALIZING---` (rendered as centered `----  GREETER_UI_INITIALIZING  ----` filling width), `◈ [BLUME_IDP] using Protocol::CTOS_LOCKD|CTOS_GREETD|CTOS_TEST|CTOS_DEFAULT`, `[BLUME_IDP] Authentication Session opened.`, success `[BLUME_IDP] IDENTITY_VERIFIED // SID:<24 hex>` + `[BLUME_IDP] Authentication session closed.`, failure `[SENTINEL] Authentication Failed (TraceId: <16 hex>)`. Prompt prefix `"» "`. Command errors prefixed `ERR`; help `commands: change, chusr, chdesk, users, desktops, help`. Hex is uppercase (Faker.randomHexString).
- Clock format `hh:mm`; date `dddd dd MMMM` (Qt, not uppercased).

### 2.4 Text effects
- Typewriter (Typewriter.qml): overwrites old text left-to-right; `charIndex` animates over `nextText.length * 10` ms, `Easing.InSine`; queued strings drained by a 500 ms repeating Timer; shorter replacement padded with spaces.
- Terminal prompt synthetic command: `charIndex` 0→len, `Easing.Linear`, duration not set (Qt NumberAnimation default 250 ms).
- Disclaimer erase: characters removed via `substring(0, charsShown)` animation (see Motion).
- "Scrolling values": ScrollableValue (bar MIC/VOL) changes value by mouse wheel ±1, Shift ±10; hover recolors.
- Password glyph `█`; cursor glyph `▁`; dots `"."` repeat 1–3.

---

## 3. Geometry

### 3.1 Bar (UP/bar.qml and components)
- PanelWindow anchored top/left/right, `implicitHeight: 37`, color Theme.background.
- Full-bar 1px border: Rectangle `border.width 1`, `border.color Theme.ctosGray`, fill transparent.
- RowLayout `spacing: 0`. Left Row, flexible spacer, right Row.
- **Divider**: Rectangle width `1`, color Theme.secondary (#7a7a7a), `topMargin 1`, `bottomMargin 1` (inside bar border), full height.
- **CornerFrame**: `thickness 1`, `armLength 7`, `horizontalMargin 4`, accentColor `"white"`. Four L-corners (two Rectangles each: armLength×thickness and thickness×armLength) at TL (0°), TR (90°), BR (180°), BL (270°). implicitWidth = content + 2·7 + 2·4; implicitHeight = content + 2·7. Content Row at x = armLength+horizontalMargin (11), y = armLength − 2 (5, "visual fix").
- Segments (Meter, SimpleSegment, Status, Battery): Item `height: parent.height`, `width: frame.width + 6`; CornerFrame centered with `height: parent.height - 6` (= 31).
- **SystemLabel**: gray block Item width `105`, full height, fill Theme.ctosGray. distro-arch.svg left `leftMargin 8`, vertically centered, size `root.height * 0.8` square (≈29.6). "CT" right-anchored `rightMargin 2`, baseline at bottom `baselineOffset -5`. Then "OS" text outside the block, vertically centered, `rightPadding 2`.
- **Meter**: ColumnLayout `width 90`, `y -2`, `spacing 1`; top RowLayout `spacing 2` [name][symbol][fill][value]; bar Rectangle `preferredHeight 3`, track Theme.secondary, fill Theme.ctosGray scaled `xScale = clamp(pct,0,100)/100` from `origin.x 0`. Computed width: 90+22 frame +6 = 118 px. Continuous fill, **no segments**.
- **SimpleSegment**: RowLayout `spacing 5` [icon box][text]; icon box `text.height - 2*vh` square.
- Net icon: Accents (`animate false`, `finalHorizontalOffset -2`, `finalVerticalOffset -2`, `individualSize 2`) around a circle `height = parent.height*0.4`, `radius height/2`, color success when connected else secondary. **Only radius in the whole QML codebase.**
- User icon: `▨`, `verticalCenterOffset -1`.
- **Workspaces**: Row, `count 5`, `width: parent.height * 5 + (count - 1)`; each Repeater cell = [Workspace][Divider]. **Workspace**: square `width = height = parent.height` (37); CornerFrame `width/height parent.height - 6` (31) centered; active marker = crosshair of two Rectangles `9×1` and `1×9`, Theme.ctosGray, centered; opacity 1 active / 0 inactive (user: 0.35 on hover, pointing-hand cursor, click dispatches `hl.dsp.focus({ workspace = N })`).
- **Status**: RowLayout `spacing 0`; ScrollableValue RowLayout `spacing 0`.
- Bar order (left→right): SystemLabel, Divider, Workspaces (5×[ws, divider]), CPU Meter, Divider, MEM Meter, Divider, ‹spacer›, Net SimpleSegment, Divider, User SimpleSegment, Divider, [user: Battery, Divider(visible with battery)], Status.

### 3.2 bar/resources SVGs (not referenced by any QML)
- corner-square.svg: 10×10 viewBox; `<rect x=0.5 y=0.5 width=9 height=9>` stroke `#d9d9d9`, default stroke-width 1, no fill.
- corner-frame.svg: 10×10; path `M 10,0.53 L 0.533,0.497 L 0.515,9.996` (top + left edges of an L), stroke `#d9d9d9`, width 0.999993, no fill.
- distro-arch.svg: 34×34; Arch-style "A" from 6 `<line>`s stroke `#0E0E0E` (width default 1) plus six 3.4×3.4 squares `#0E0E0E` at apex (15.3,0), feet (0,30.6) and (30.6,29.75), inner notch (11.9,24.65), (18.7,24.65), (15.3,17).

### 3.3 Accents (UP/common/components/Accents.qml)
- 4 Images of accent.svg, `individualSize 4` (default), rotations TL 0, TR 90, BR 180, BL 270.
- Final position offset outward: `_horizontalOffset = -startingH - finalH`, defaults `finalHorizontalOffset 18`, `finalVerticalOffset 10`, starting 0.
- accent.svg: 4×4, filled quarter-disc (pie, radius 4, right angle at the outer corner), `#ffffff` opacity 0.95.
- Overrides: Time.qml 110×110 box, start −20/−20, final 20/20; Terminal.qml defaults offsets; bar net icon −2/−2 size 2.

### 3.4 Greeter layout (MainLayout.qml)
| element | geometry |
|---|---|
| background | lock.png `anchors.fill` (user: `PreserveAspectCrop`) |
| Splash | `294*vh × 48*vh`, h-centered, verticalCenter at `root.height * 0.406` |
| Accents | anchored to splash (state "splash"); state "field_group" exists but is never set |
| FieldGroup | top = splash.bottom + `50*vh`, h-centered, width `294*vh` |
| Disclaimer | left/right = splash, top = splash.bottom + `(25+50+85+15)*vh`, `leftMargin 2` |
| Time | top-left, margins `root.height * 0.05` both |
| Terminal | bottom-left, `leftMargin round(width*0.037)`, `bottomMargin round(height*0.046)`, width `94.5 * rem` (rem = advance of "-") |
| Session | top-right, `rightMargin height*0.0375`, `topMargin height*0.046` |
| DeviceId | bottom-right, height `root.height*0.45`, `rightMargin height*0.0375`, `bottomMargin height*0.046` |

- **Splash**: gray Rectangle (ctosGray) fills the box, scaled `xScale 0.73` (final) from left. "OS" width `0.26 * width`, right-anchored, baseline `bottom − 3`. "CT" width `os.width/2`, `leftMargin = -tightBoundingRect.x + 0.58*width`, baseline `bottom − 5`, color background (dark text on gray).
- **FieldGroup**: ColumnLayout `spacing 0`. Row `spacing 5`: barcode.svg `preferredHeight 10` (PreserveAspectCrop) + "PASSPHRASE". PasswordField `preferredHeight 40*vh`, bg Theme.background, `border 2` ctosGray, `leftPadding 8`, `rightPadding cursor width + 6`; progress fill Rectangle (ctosGray, xScale=pct/100). Under field: "INITIALIZING…" left, percent right, `topMargin 5*vh`. LOGIN Rectangle `preferredHeight 26*vh`, width `parent.width*0.38`, right-aligned, ctosGray fill.
- **Spinner**: Grid 3×3, `spacing 3`, squares `size 5`, color Theme.background, center cell transparent.
- **Time**: Row `spacing 20` [Accents 110×110 with os-icon.svg fill] [Column `spacing 12`: clock, date pill]. Pill width = label + 24, height = label + 6, `border 1 #414141`, transparent, label `leftMargin 8`, `verticalCenterOffset 2`.
- **Terminal**: ColumnLayout `spacing 15*vh`, `margins 10` (property, unused in layout), lineHeight = textMetrics.height + `5*vh`, maxLines = clamp(floor((Screen.height*0.25 − (version.height + spacing)) / lineHeight), 0, 10); list keeps last 50 entries. Version box: height `round(version.height + 8)`, `border 1` darker(white,1.6), transparent, text `leftMargin 10`, wrapped by Accents.
- **Session**: RowLayout `spacing 0`: [ctosGray square `56*vh` with blume-logo.svg, margins `2*vh`] [column `150*vh` wide: top bar `28*vh` backgroundBright with margins `10*vh` holding username Typewriter + tesseract `15*vh`; bottom bar `28*vh` ctosGray with user-barcode.svg margins `5*vh`] [picture square `75*vh`, Theme.secondary fill, user.svg].
- **DeviceId**: Row: aside ColumnLayout width `35*vh`, `topMargin 5*vh`: tesseract (size deviceText.width + 2), spacer, device-text.svg (vertical, `topMargin 20*vh`, `bottomMargin 5`, PreserveAspectFit), bottom rule `height 3` textPrimaryDim. Then device-barcode.svg full height (PreserveAspectFit, z 3).
- **Disclaimer**: RowLayout `spacing 8`: tesseract.svg 32×32 top-aligned + two text lines.
- **Surface** (used by unused Status): side bars `barWidth 3`, center fill `#14ffffff`, Status margins 18 / top 14 / bottom 14, InfoField row spacing 30, column spacing 10.
- **IdentityCard** (unused): left column 55% width, spacing 15, row spacing 10; picture width = height·4/5.
- **ThinProgress** (unused): bar `5*vh` high, `topMargin 5*vh`.

### 3.5 Radius
QML: only the net-status dot (`radius: height/2`). Everything else is square-cornered. User configs: Hyprland `rounding = 0`, mako `border-radius=0`, fuzzel `radius=0`, rofi none set.

---

## 4. Motion

### 4.1 Bar
- Meter fill: `Behavior on xScale` → NumberAnimation `1000` ms `Easing.InOutSine`.
- CPU sample every 2 s; MEM upstream every 1 s (`sleep 1`), user patch 2 s (Timer 2000). Workspace id debounce Timer `10` ms.
- **CTOS_BAR_ANIMATIONS** (not upstream; user patch `bar/config/BarSettings.qml` in OUT/ctos-b1t.patch): `Env.get("BAR_ANIMATIONS") || "all"`, lower-cased; `animationsEnabled = (value === "all")`; Meter Behavior gets `enabled: BarSettings.animationsEnabled`. So `reduced` and `none` both make the meter bar jump with no tween. User launches with `env CTOS_BAR_ANIMATIONS=reduced` (USR/.config/hypr/hyprland.lua line 61, USR/.local/bin/ctos-bar). Note: user's uploaded Meter.qml/BarSettings.qml are not in the upload set; the patch is the source.
- Net icon Accents `animate: false` (jumps to final).

### 4.2 Greeter animation profiles (greeter/config/Settings.qml, GeneralDto.qml)
- Enum None=0, Reduced=1, All=2; `animationProfile(m) = animationMode >= m`. Unknown → All. GeneralDto global default `"all"`; per-mode default `"none"`; lockd default `"reduced"`. Schema enum `["reduced","all"]` ("'reduced' for no splash").
- Every greeter reveal (Accents, Splash, MainLayout startSplash/startupAnimation, Time, DeviceId, Surface) runs only at `All`. At Reduced/None everything renders in final state; terminal is manually resumed. IdentityCard reveal is `running: true` regardless (component unused). Exit sequence on success and Spinner/cursor/typewriter always run.

### 4.3 Greeter sequence (ms, easing)
1. Terminal prints boot lines with random delay clamp(random·400, 200, 400) per line; pauses at `UI_INIT`.
2. startSplash: disclaimer opacity 0→1 `100` InCubic ∥ Accents.start().
3. Accents: opacity 0→1 `50` InCubic ∥ margins to final `100` InCubic (Terminal/Time Accents: opacity `200`, translate `300`).
4. Splash.startupAnimation: setup xScale 0, yScale 0.7, CT/OS hidden → pause `500` → xScale→0.4 `700` (default easing Linear) → emits progressBarMidway (starts Time + DeviceId) → xScale→1 `300` OutSine → pause `300` → yScale→1 `350` OutCubic → pause `500` → OS opacity 1 (`0`) → xScale→0.73 `300` OutQuart → CT opacity→1 `100` → revealFinished.
5. MainLayout startupAnimation (splash starts at verticalCenterOffset = height/2): splash offset → `height*0.406` `500` InOutCirc ∥ disclaimer topMargin from `25*vh` to final `500` InOutCirc ∥ (pause `300`, fieldGroup opacity→1 `200` OutExpo) → terminal resumes.
6. Time: (after its Accents finish) pause `100` → logo opacity `300` InExpo ∥ (pause 100, clock opacity + y −10→0 `300` InExpo) ∥ (pause 200, date pill opacity + y −10→0 `300` InExpo).
7. DeviceId: barcode+rule opacity→1 `300` **InBounce** → tesseract opacity + x 10→0 `300` InExpo → device-text opacity + x 10→0 `300` InExpo.
8. Auth: on submit state Loading, response after Timer `1000`; failure → retry Timer `500`.
9. Success exit: pause `200` → Disclaimer.exit (line 2 chars→0 `300` InQuart ∥ (pause 50, line 1 chars→0 `300` InQuart) → pause `25` → icon opacity 0 `25`) → pause `200` → FieldGroup: pause `200`, clear field, PASSPHRASE row+LOGIN text opacity 0 `150` ∥ LOGIN yScale→0 `275` OutCubic (origin top-right) → field border color → secondary `200` ∥ field height → `4*vh` `300` OutCubic ∥ (pause 150, progress labels opacity→1 `150`, start dot spinner) → pause `400` → progress→40 `1000` InSine → pause `300` → progress→100 `300` InSine → AuthManager.finish.
- Accents state transition: AnchorAnimation `400` InOutCirc (never triggered).
- Splash exit (defined, never called): CT opacity→0 `200` OutCubic ∥ xScale→1 `500` OutCubic → pause 400 → OS hidden → progress opacity→0.1 `250` InOutCirc ∥ yScale→0.075 `250` OutCirc → pause 300.
- Surface reveal: side bars yScale 0→1 `320` InOutCirc → pause `200` → bars translate to edges + center/content xScale 0→1 `400` OutQuint.
- IdentityCard: opacity `150`, then values fade `150` each (class ∥ name after 50).
- ThinProgress: % fade `200`; 0→0.3 `500`; pause 200; →1 `500` InQuad.
- **Spinner**: `delay 100` ms per step, infinite, 8 steps clockwise around the ring (0,0)→(1,0)→(2,0)→(2,1)→(2,2)→(1,2)→(0,2)→(0,1), each step fades current in / previous out in parallel; opacity 1 while active.
- **Text dots**: dotCount 1→3 over `900` ms, infinite.
- **Cursor blink**: hold 1 `500`, 1→0 `300`, hold 0 `300`, infinite; restarts on each keystroke.
- **Terminal scroll**: contentY Behavior `50` ms OutCubic (`0` for instant messages).
- **Typewriter**: `len*10` ms InSine per string, queue tick `500` ms.

### 4.4 Hyprland (USR/.config/hypr/hyprland.lua)
Curves: easeOutQuint (0.23,1)(0.32,1); easeInOutCubic (0.65,0.05)(0.36,1); linear (0,0)(1,1); almostLinear (0.5,0.5)(0.75,1); quick (0.15,0)(0.1,1); spring easy mass 1, stiffness 238.1191, dampening 24.21279333.
| leaf | speed | curve | style |
|---|---|---|---|
| global | 10 | default | |
| border | 5.39 | easeOutQuint | |
| windows | 4.79 | spring easy | |
| windowsIn | 4.1 | spring easy | popin 87% |
| windowsOut | 1.49 | linear | popin 87% |
| fadeIn / fadeOut | 1.73 / 1.46 | almostLinear | |
| fade | 3.03 | quick | |
| layers | 3.81 | easeOutQuint | |
| layersIn / layersOut | 4 easeOutQuint / 1.5 linear | | fade |
| fadeLayersIn / Out | 1.79 / 1.39 | almostLinear | |
| workspaces / In / Out | 1.94 / 1.21 / 1.94 | almostLinear | fade |
| zoomFactor | 7 | quick | |

---

## 5. Components (structure, strings, states)

### Bar
- **SystemLabel**: `[▮ gray block 105px: arch glyph left, "CT" dark bottom-right][ "OS" light 36px ]` (a horizontal "ct|OS" lockup like the logo).
- **Workspaces**: 5 bracketed squares separated by 1px dividers; active = gray crosshair "+" (9px); inactive = empty bracket (user: 35% crosshair on hover, clickable).
- **Meter (CPU / MEM)**: bracketed box; row `CPU <>        012%` / `MEM ##        3.4G` at 12px/600 white; below a 3px track (#7a7a7a) with #D9D9D9 fill proportional to usage.
- **Net**: bracketed `[• SSID___]` with green dot (connected) or gray (`--N/A--`); dot framed by 2px accent quarter-discs.
- **User**: bracketed `[▨ H4CH1]`.
- **Battery** (user only): bracketed `BAT-087`; label gray, number white / green (charging) / red (≤15% on battery); hidden without battery.
- **Status**: bracketed `071026-1432-AEDT-1234-MIC045VOL060` with gray/white alternation: date gray, `-hhmm-` white, tz/host gray, `MIC`/`VOL` gray (white on hover) + values gray (green on hover). Click mutes, wheel adjusts.

### Greeter
- **Splash**: gray progress bar with dark "CT" at its right end and large thin white "OS" after it (same lockup as logo), framed by 4 accent dots.
- **FieldGroup**: "PASSPHRASE" with a small barcode, a 2px-bordered black input showing `█` blocks, a gray "LOGIN" tab under its right end; states color the text: Ready white, Loading #CACACA (+spinner replaces LOGIN), Success/Finish green, Failed red. On success collapses to a thin bar that fills with gray while "INITIALIZING..." and a percentage appear.
- **Disclaimer**: tesseract icon + 2 lines of 11px gray legal text.
- **Time**: os-icon diamond in 110px accent frame, big thin clock, bordered date pill.
- **Terminal**: fake log at bottom-left, `»` prompt with interactive input (commands change/chusr/chdesk/users/desktops/help), version line in a 1px box with accent corners.
- **Session**: ID-badge strip top-right: gray hex-cube logo tile, dark username bar with tesseract, gray barcode bar, gray avatar square.
- **DeviceId**: vertical strip bottom-right: tesseract, vertical device text, 3px rule, tall vertical barcode (matches the wallpaper side strip).
- Unused in layout: IdentityCard (EMPID ##/CLASS/FULL NAME + id-barcode card), Status (BATTERY|ENV / NODE), ThinProgress, arrow.svg. `Settings.fakeIdentity`/`fakeStatus` are not defined in Settings.qml.

---

## 6. Iconography / motifs (greeter/resources)
| file | size | depicts | style |
|---|---|---|---|
| accent.svg | 4×4 | quarter-disc corner tick | fill #fff @0.95 |
| arrow.svg | 16×16 | solid upward triangle | fill #0e0e0e (unused) |
| barcode.svg | 62.36×16.21 | 2-row white block barcode | fill #fff |
| blume-logo.svg | 48×48 (12.7 mm) | hexagon of 3 isometric cubes (Blume) | stroke #0e0e0e w 0.396875, no fill |
| device-barcode.svg | 35×681 | tall vertical barcode/2D-code strip | fill #B6B6B6 |
| device-text.svg | 14×418 | vertical text `SYD-AU-NSW-02|8D5A6D11-7C25-4442-BCAA-81749930DD9F` (outlined paths, reads bottom→top) | fill #CACACA |
| id-barcode.svg | 241×72 | ID card: #B6B6B6 72×72 tile with hex-cube logo (stroke #0E0E0E w 2, miterlimit 2.6), light panel with code `7D2A12F9B1SDFA4`, diamond glyph, barcode | #B6B6B6/#D9D9D9/#E6E6E6/#0E0E0E |
| os-icon.svg | 136×136 | ctOS diamond: outer square rotated 45°, vertical center line, inner V facets | fill white, thin outlines (~0.86 px in 136 box) |
| tesseract.svg | 122×122 | wireframe octahedron/diamond with two filled small diamonds (front opaque, back face opacity 0.612) | fill #fff |
| user.svg | 176×176 | generic head-and-shoulders silhouette | fill #cdcdcd |
| user-barcode.svg | 195×27 | 1D barcode, bars 27 or 23.8235 tall | fill #0E0E0E |
| lock.png | 2560×1440 | see §8 | |

User's app icons (OUT/ctos2/icons/*.svg, 64×64): tile `rect 3,3 58×58` fill `#0E0E0E` stroke `#7A7A7A` 1; corner brackets path `M3 15V3h12 M49 3h12v12 M61 49v12H49 M15 61H3V49` stroke `#D9D9D9` 2 (12px arms); glyph strokes `#D9D9D9` 2.5 (miter joins) or 3.5–4 for bars; secondary strokes `#7A7A7A` 1.5–2; single accent pixel/square `#00FA9A` (monitor 2×2, network 4×4).

---

## 7. Terminal and desktop configs

### 7.1 Kitty Monoglow palette
Upstream UP/extras/.config/kitty/themes/monoglow_z.conf; user USR/.config/kitty/themes/ctos.conf (same file + changes marked *).
| key | upstream | user |
|---|---|---|
| background | #121212 | #121212 |
| foreground | #cccccc | #cccccc |
| selection_background / _foreground | #2a2a2a / #dddddd | same |
| cursor / cursor_text_color | #cccccc / #121212 | same |
| url_color | #708090 | same |
| active_border_color | #1bfd9c | *#00FA9A |
| inactive_border_color | not in source | *#2a2a2a |
| bell_border_color | not in source | *#fc3e38 |
| active_tab bg/fg | #dddddd / #0e0e0e | same |
| inactive_tab bg/fg | #2a2a2a / #7a7a7a | same |
| tab_bar_background / margin_color | none / none | same |
| color0 / color8 | #2a2a2a / #4a4a4a | same |
| color1 / color9 | #deeeed / #708090 | same |
| color2 / color10 | #1bfd9c / #a6ffc9 | *#00FA9A / #a6ffc9 |
| color3 / color11 | #b4b4b4 / #dddddd | same |
| color4 / color12 | #7a7a7a / #aaaaaa | same |
| color5 / color13 | #66b2b2 / #49c4c4 | same |
| color6 / color14 | #cccccc / #d3d3d3 | same |
| color7 / color15 | #f1f1f1 / #ffffff | same |
| mark1 fg/bg | – | *#121212 / #00FA9A |
| mark2 fg/bg | – | *#121212 / #dddddd |
| mark3 fg/bg | – | *#121212 / #fc3e38 |

kitty.conf upstream: `font_family family="JetBrainsMono Nerd Font Mono"`, `font_size 13.0`, bold/italic auto, `window_padding_width 12`, `map ctrl+shift+h no_op`.
kitty.conf user: font same family, `font_size 12`; `cursor_shape beam`, `cursor_beam_thickness 1.5`, `cursor_trail 1`, `cursor_trail_decay 0.1 0.4`, `cursor_trail_start_threshold 2`; `background_opacity 0.85`, `background_blur 32`, `dynamic_background_opacity yes`, `inactive_text_alpha 0.6`; `scrollback_lines 20000`; `window_padding_width 14`, `window_border_width 1pt`, `draw_minimal_borders yes`, `initial_window_width 120c`, `initial_window_height 34c`, `visual_bell_duration 0.25`; tabs `tab_bar_style powerline`, `tab_powerline_style slanted`, `tab_bar_min_tabs 2`, `tab_bar_edge top`, `tab_bar_margin_height 6.0 0.0`, title `" {index}:{title} "`, active bold; `url_style curly`; `enabled_layouts splits, stack`; `shell ~/.local/bin/ctos-shell`. Cheatsheet colors: gray `38;2;122;122;122`, white `38;2;204;204;204`.

### 7.2 Fastfetch
Upstream (UP/extras/.config/fastfetch/config.jsonc): logo file `dedsec.txt` (18-line `#` ASCII DedSec mask), padding top 1 / left 7 / right 10; key color `#f1f1f1`; title `{#white}tom-smith{#}{#blue}@{#}{#green}au-south-east-2-014-493{#}`; separator; OS key "OS" format `ctOS-0.1.0-a`; Kernel format `blume-krn-1.0.8`; uptime, packages, break, shell, de, wm, icons, font, terminal, terminalfont, custom Theme `wnkz/monoglow.nvim`, break, disk, break, localip, battery, poweradapter, break, colors.
User (USR/.config/fastfetch/config.jsonc): logo `type: file`, color `"1": "38;2;241;241;241"` (dedsec.txt gets `$1` prefix on line 1), same padding; keys `#f1f1f1`; title `{#white}{user-name}{#}{#blue}@{#}{#38;2;0;250;154}{host-name}{#}`; plain `os`/`kernel` (no fake strings); rest identical.

### 7.3 ctos-banner (USR/.local/bin/ctos-banner)
Colors: `g` 202,202,202; `d` 122,122,122; `w` 255,255,255; `ok` 0,250,154.
```
┌─[ ctOS ]──────…  (top: "┌─[ ctOS ]" + 40 ─, d/w)
│ ▨ user@host   ● ONLINE     (g, ok)
│ UPTIME <uptime -p>          (d label, g value)
│ TIP    <random tip>
└────… (50 ─)
```

### 7.4 Powerlevel10k (USR/.p10k.zsh)
Wizard: nerdfont-v3 + powerline, small icons, unicode, **lean**, 24h time, 2 lines, disconnected, no frame, sparse, many icons, concise, transient_prompt. Left: os_icon, dir, vcs / newline / prompt_char (`❯` ins, `❮` cmd, `V` vis, `▶` overwrite). Right starts with status, command_execution_time, background_jobs, … context, … time (`%D{%H:%M:%S}`). `POWERLEVEL9K_BACKGROUND=` (transparent), `ICON_PADDING=none`, `PROMPT_ADD_NEWLINE=true`, `TRANSIENT_PROMPT=always`.
ctOS overrides (end of file): OS_ICON `#D9D9D9`; DIR `#CACACA`; DIR_SHORTENED `#7A7A7A`; DIR_ANCHOR `#FFFFFF`; VCS icon `#7A7A7A`, clean `#00FA9A`, untracked/modified `#CACACA`, conflicted `#FC3E38`; PROMPT_CHAR ok `#00FA9A`, error `#FC3E38`; STATUS ok/ok_pipe `#00FA9A`, error/signal/pipe `#FC3E38`; CONTEXT default `#7A7A7A`, root `#FC3E38`; TIME, COMMAND_EXECUTION_TIME, BACKGROUND_JOBS, VIRTUALENV, NODE_VERSION, NVM `#7A7A7A`; MULTILINE_FIRST_PROMPT_GAP `#202020`.

### 7.5 Rofi (USR/.config/rofi/ctos.rasi; config OUT/ctos2/rofi/config.rasi)
Vars: bg `#0E0E0Eee`, panel `#202020`, fg `#CACACA`, dim `#7A7A7A`, line `#D9D9D9`, ok `#00FA9A`. Font `JetBrainsMono Nerd Font 12`; display-drun `"ctOS"`, run `"run"`, window `"win"`, icons on (hicolor).
window: center, `width 560px`, bg @bg, `border 1px` @line. mainbox `padding 14px`, `spacing 12px`. inputbar `padding 8px 10px`, `border 0 0 1px 0` @dim, spacing 10px; prompt @ok; entry placeholder `"SEARCH"` @dim, text `#FFFFFF`. listview `lines 8`, 1 column, no scrollbar, spacing 2px. element `padding 8px 10px`, spacing 12px, `border 0 0 0 2px` transparent; selected bg @panel, left border @line, text `#FFFFFF`. element-icon `26px`. ctos-clip override: `window {width: 700px;}`, icons off, prompt "clip".

### 7.6 Mako (USR/.config/mako/config)
`font=JetBrains Mono 11`, `background-color=#0E0E0Eee`, `text-color=#CACACA`, `border-color=#D9D9D9`, `border-size=1`, `border-radius=0`, `padding=12`, `margin=12`, `width=360`, `default-timeout=6000`, `anchor=top-right`, `layer=overlay`; low urgency border `#202020`; critical border `#FC3E38`, timeout 0. OSD notifications (ctos-vol/ctos-bright) use `-t 1200` with progress `int:value`.

### 7.7 Fuzzel (USR/.config/fuzzel/fuzzel.ini, fallback launcher)
`font=JetBrains Mono:size=12`, `prompt="> "`, `width=40`, `lines=10`, `horizontal-pad=24`, `vertical-pad=18`, `inner-pad=10`, `line-height=22`, `letter-spacing=0`, layer overlay. Colors: background `0E0E0Eee`, text `CACACAff`, prompt `D9D9D9ff`, placeholder `7A7A7Aff`, input `f1f1f1ff`, match `ffffffff`, selection `202020ff`, selection-text/match `ffffffff`, border `D9D9D9ff`. Border `width=1`, `radius=0`.

### 7.8 Hyprland (USR/.config/hypr/hyprland.lua = OUT/ctos2/v15)
Monitor eDP-1 preferred, scale 1.25. general: `gaps_in 5`, `gaps_out 12`, `border_size 2`, active_border gradient `rgba(D9D9D9ff)` → `rgba(7A7A7Aff)` angle 45, inactive_border `rgba(202020ff)`, layout dwindle. decoration: `rounding 0`, `rounding_power 2`, active/inactive opacity 1.0, shadow enabled range 4, render_power 3, color `0xee000000`; blur enabled size 6, passes 2, vibrancy 0.1696. Animations: §4.4. Cursor size 24. (Earlier `hyprland.lua.cur` had green/cyan gradient, gaps_out 20, rounding 10, now replaced.)
hypridle: lock at 300 s and before sleep (`ctos-lock` = `env CTOS_MODE=lockd quickshell -p /opt/ctos/greeter.qml`), DPMS off at 600 s.
Wallpaper (ctos-wallpaper): AC → `mpvpaper` loop of `~/Pictures/ctos-live.mp4`; battery → `swaybg -m fill -i ~/Pictures/ctos-wallpaper-v1.png`.
Screenshot region (ctos-shot): `slurp -b 0e0e0eaa -c d9d9d9ff -s 00000000 -w 1`.

### 7.9 Zed (USR/.config/zed/settings.json + themes/ctos.json)
UI font `JetBrainsMono Nerd Font` 15; buffer `JetBrainsMono Nerd Font Mono` 14; cursor_blink false. Theme "ctOS" (dark): background `#0e0e0e`, editor/panel/surface `#121212`, borders `#2a2a2a`/`#202020`, focused `#d9d9d9`; text `#cccccc`, muted `#7a7a7a`, placeholder `#555555`; ANSI = user kitty palette; syntax: keyword `#ffffff` 700, string `#a6ffc9`, escape/regex `#00FA9A`, number/boolean/constant `#dddddd`, function `#ffffff`, type `#d9d9d9`, comment `#7a7a7a` italic, operator/punctuation `#7a7a7a`, attribute `#aaaaaa`, link_uri `#708090`.

---

## 8. Images
- **.assets/Logo.png** (960×227): dithered near-black field (#0c–#12) with faint grid; centered lockup: a solid `#d9d9d9` bar (x 284–570, y 83–148 → 286×65 px) with dark "CT" set at its right end, followed by tall thin white `#ffffff` "OS" (≈ x 583–696). Four tiny 4×4 light accent ticks at the corners of the lockup's bounding box (≈ (264,69), (692,69), (264,158), (692,158)), i.e. the Accents motif. Proportions match the Splash (bar 0.73, OS 0.26).
- **greeter/resources/lock.png** (2560×1440): plain dithered near-black (mode #0f0f0f, range #0a–#13) with a barely visible square grid: minor lines every 20 px, brighter major lines every 160 px (≈ +1 to +3 luminance over base). No other content (greeter draws everything on top).
- **extras/wallpapers/wallpaper-v1.png** (2560×1440): same grid background. Centered os-icon/tesseract-style diamond: outline square rotated 45°, bbox x 950–1610, y 390–1050 (660 px, centered at 1280,720), stroke ≈4 px `#d9d9d9`, vertical line from top vertex to inner diamond, two facet lines from left/right vertices to a filled `#d9d9d9` inner diamond (≈236 px wide at y 845) in the lower half, plus lines to the bottom vertex. Right edge (x ≈ 2445–2517, y 708–1389): vertical device-ID strip identical to DeviceId: small diamond glyph at top, `#b6b6b6` vertical 2D barcode blocks, rotated text `SYD-AU-NSW-02|8D5A6D11-7C25-4442-BCAA-81749930DD9F` (reads bottom→top), thin rule at the bottom-left of the strip.
- **extras/wallpapers/wallpaper-v2.png** (2560×1440): same background and right-edge ID strip; the diamond is small (160 px, bbox x 79–239, y 1200–1360) in the bottom-left corner instead of center.
- User live wallpaper preview (OUT/ctos2/live-preview.png): two frames of the v1 composition with slightly varying background luminance (animated loop `ctos-live.mp4`).

---

## 9. User deviations from upstream (summary)
1. accentGreen `#1bfd9c` → `#00FA9A` (Theme.qml, kitty color2/active border, all configs).
2. Bar `focusable: false`; CPU/MEM via FileView + 2 s Timer; wired detection `DeviceType.Wired`; Battery segment added; Status tz/hostname instead of `AEDT-1234-`; clocks at minute precision; MIC/VOL rounded.
3. `CTOS_BAR_ANIMATIONS` (all|reduced|none) gating the meter tween; user runs `reduced`.
4. Workspace hover preview 0.35 + click-to-focus.
5. Greeter: terminal first line shows hostname; lock.png PreserveAspectCrop; security hardening (no test fallback, debug keys test-only).
6. Kitty: font 12, padding 14, 0.85 opacity + blur 32, beam cursor with trail, marks, inactive/bell borders.
7. Hyprland: gray gradient borders, square corners, gaps 5/12, border 2.
