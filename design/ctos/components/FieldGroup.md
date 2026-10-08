The lock screen passphrase group: a barcode and `PASSPHRASE` label, a 40px field with a 2px `ctosGray` border, and a `ctosGray` LOGIN tab under its right end (38% wide, 26px tall).

- The field masks input with `█` and blinks a `▁` cursor (500ms on, 300ms fade, 300ms off). Text is 16px with 5px letter spacing.
- States change the text color: ready `textPrimary`, loading `textPrimaryDim` with the 3 by 3 spinner in place of LOGIN, failed `error`, success `success`.
- On success the label and LOGIN fade, the field collapses to a 4px bar with a `textSecondary` border, `INITIALIZING` with cycling dots appears with a percentage, and the bar fills `ctosGray` to 40% over 1000ms, then to 100% over 300ms.
- A failed attempt logs `[SENTINEL] Authentication Failed (TraceId: ...)` in the terminal.

The consumer provides PAM (the `login` service on b1t, with the user set).
