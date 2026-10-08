The ctOS lock screen (`CTOS_MODE=lockd`), laid out on `lock.png`, the dithered grid background. Sizes scale with screen height (`vh = height / 1080`).

- Top left, 5% in: the ctOS diamond in a 110px accent frame, the clock `hh:mm` (`clock`, 48/100, `textPrimaryDimmer`) and a date pill (`dddd dd MMMM`, 14/300 `date-text`, 1px `date-border`).
- Center: the lockup at 40.6% of the height, the passphrase group 50vh below it, the two-line Blume disclaimer with the tesseract under that.
- Bottom left: the boot log in `textPrimaryDimmer` (`REGION_LINK_ESTABLISHED`, `LOG_STREAM_CONNECTED`, `WL_OUTPUT_FOUND`, `GREETER_UI_INITIALIZING`, `[BLUME_IDP] using Protocol::CTOS_LOCKD`), the `» ` prompt, and the version box `blume-krn-1.0.8 <> ctOS-1.0.0-a` in a 1px box with accents.
- Top right: the ID badge (Blume logo tile, username bar on `backgroundBright` with the tesseract, barcode bar on `ctosGray`, `textSecondary` picture square).
- Bottom right: the device strip (tesseract, vertical device text, a 3px rule, the tall barcode), the same strip as the wallpapers.

b1t runs the lock in reduced mode: no splash, everything appears in its final state. The terminal accepts `change`, `chusr`, `chdesk`, `users`, `desktops` and `help`.
