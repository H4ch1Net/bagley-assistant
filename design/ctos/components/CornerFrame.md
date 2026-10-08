CornerFrame is the bracket that holds every bar segment: four white L-corners, 7px arms, 1px thick, 4px from the content.

- Size: content width plus 22px wide (two 7px arms and two 4px margins), and `bar-height` minus 6px (31px) tall. The content sits 11px in and 5px down.
- Corners are `textPrimary` white even though the bar outline is `ctosGray`.
- Accents are the lock screen's version: 4px white quarter discs (`accent.svg` at 0.95) placed 18px and 10px outside an element's four corners. They frame the lockup, the 110px clock icon (offset 20px) and the terminal version box. On the network dot they shrink to 2px and sit 2px out.
- Color carries state only on the value: `error` for a low battery, `success` while charging. The label stays `textSecondary`.
