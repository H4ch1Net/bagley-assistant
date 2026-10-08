Bagley's avatar is a small node graph seen through a tracking overlay: a ctOS diamond hub (`00`) and square satellite nodes inside a square viewport with a faint grid and corner brackets.

- Spine edges join each node to the hub and dashed mesh edges join neighbours. Signal packets (small squares with a short tail) travel along them, and a node brightens when one lands.
- Every node has a tracking box that lags slightly behind it: corner brackets on the hub, thin squares on the satellites, each with its two-digit ID.
- Corner readouts: the state code with a status square top left, `ID00` and its confidence (or the running tool's name) top right, the hub position bottom left, `SIG nn` (packets in flight) and `TRK nn` (tracked nodes) bottom right.
- States: IDLE slow trickle out of the hub; INPUT traffic flows into the hub and the graph leans toward the message box; THINK eight nodes, full mesh, dense traffic; REASON signals pass along chains; EXEC a scan line, the tool name, traffic aimed at one node; AWAIT no traffic, the hub blinks; TX and VOICE each word fires the hub; DONE one lock-on box around the graph; ERROR edges drop out and boxes jitter; NO SIGNAL dashed, dimmed, no traffic.
- In ctOS colors the accent is `ctosGray`, DONE uses `success`, ERROR uses `error`, AWAIT blinks `textPrimary`. In the Bagley app it uses the app's accent color, OK color and warning color.
- Sizes: full (140px and up, with labels), compact 36px (3 nodes) and mini 22px (2 nodes). Message headers use a static 24px glyph of the same graph.

The source is `bagley/static/js/avatar.js` in bagley-assistant. Reduced motion freezes the graph and stops the packets.
