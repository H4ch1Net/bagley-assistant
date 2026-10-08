mako notifications in the top-right corner: 360px wide, 12px padding and margin, `panel-translucent` background, 1px border, square corners, JetBrains Mono 11, `textPrimaryDim` text, 6 second timeout.

- Normal border `ctosGray`, low urgency `backgroundBright`, critical `error` with no timeout.
- Volume and brightness keys send an OSD notification with a value for 1.2 seconds. mako fills it with its default progress color, a muted blue (#5588AA). Set `progress-color=over #202020` in the mako config to bring it into the palette.
- Keep titles to one line. The body says what happened in plain words.
