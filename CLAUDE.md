# Bagley

Local-first AI assistant: FastAPI server, vanilla JS web UI with no build step, SQLite storage, Ollama or any OpenAI-compatible model server. See README.md for features and architecture.

## Commands

- Install for development: `python -m venv .venv && . .venv/bin/activate && pip install -e ".[dev,screenshots]"`
- Unit, API and WebSocket tests: `pytest`
- Browser tests: `pytest -m ui` (Playwright Chromium; the Playwright Python version must match the installed browsers)
- Lint and format: `ruff check . && ruff format --check .`
- Regenerate README screenshots: `python scripts/screenshots.py` (needs matplotlib for the chart shot); demo stack: `python scripts/screenshots.py --serve`
- Run: `bagley` (http://127.0.0.1:8765), `bagley chat`, `bagley doctor`

## Code rules

- The frontend is plain ES modules in `bagley/static/js` and one stylesheet, `bagley/static/css/app.css`. No bundler, no framework, nothing loaded from the internet (strict CSP).
- Colors come from the tokens at the top of `app.css` only. Components never use literal colors.
- Keep the public API of `avatar.js` (`Avatar`, `avatarGlyph`, `mountAvatars`, `setState`, `setTag`, `pulse`) and the existing element ids and classes the tests use.
- New behavior gets tests. Run the unit tests, the UI tests and ruff before finishing.

## Look and feel: ctOS

The UI follows the ctOS design system in `design/ctos/`. Read `design/ctos/README.md` first, then `BRAND.md`.

- Ground `#0E0E0E`, raised `#202020`, chrome `#D9D9D9`, text tiers `#FFFFFF` / `#CACACA` / `#C3C3C3` / `#7A7A7A`. The only hues are success `#00FA9A` and error `#FC3E38`, used for state only.
- One font family: JetBrainsMono Nerd Font, falling back to JetBrains Mono and the system monospace. It cannot be loaded from the web, so use local fonts only.
- Square corners everywhere. White L-corner brackets (7px arms, 1px) instead of boxed cards. 1px hairlines in `#D9D9D9` and `#7A7A7A`. No shadows, no gradients, no emoji.
- Uppercase, terse, fixed-width readouts in the ctOS voice (see BRAND.md, Content fundamentals).
- Quiet motion: short fades, no bounces. Respect reduced motion.
- Bagley's avatar is already ctOS-styled (`design/ctos/components/BagleyAvatar.md`). In a ctOS theme set `--avatar-accent` to `#D9D9D9`, `--ok` to `#00FA9A`, `--danger` to `#FC3E38`.
- ctOS has no light theme. If the light theme stays, derive it from the same grays and keep every rule above.
