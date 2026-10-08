# ctOS design reference

Bagley's look follows ctOS, the Watch Dogs-style desktop running on b1t (TSM-061/ctOS on Hyprland). Everything here was taken from the upstream ctOS source and b1t's own configs. Treat it as the source of truth for any UI work.

Read in this order:

1. `BRAND.md`: the rules (content voice, color, type, shape, motion, imagery, iconography). Start here.
2. `tokens.json`: every token with a usage note. `tokens.css` is the same as CSS custom properties plus one class per type style.
3. `components/*.md`: guidelines for each ctOS component (bar, corner frames, lockup, passphrase field, lock screen, terminal, launcher, notifications, Bagley's avatar).
4. `previews/*.html`: static reference renders of each component. Open them in a browser (they load `../tokens.css` and `../assets/`; the wallpaper and lock background load from the public ctOS repo).
5. `SPEC.md`: the raw extraction with file paths for every value, including exact animation timings, strings and geometry. Use it when a rule above is not specific enough.

Assets: `assets/` holds the ctOS motifs (diamond, tesseract, accent corner, barcodes, Blume logo, Arch glyph), the logo PNG and b1t's app icons.

The same system is published as a browsable page (private to the owner): https://claude.ai/artifact/4Usry3v9JxadHAzi2iysNC, with a prose style guide at https://claude.ai/artifact/GyUFrXnxmRP5rQvuknTTJq.
