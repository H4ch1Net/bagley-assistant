"""Capture the README screenshots, or run a demo stack for manual testing.

Both modes run Bagley against the scripted mock model server from ``tests/mock_llm.py`` with
fake weather and search services, in a throwaway data directory. Nothing touches ~/.bagley.

    pip install -e ".[dev,screenshots]"
    python scripts/screenshots.py              # writes docs/screenshots/*.png
    python scripts/screenshots.py --serve      # demo stack at http://127.0.0.1:8790
"""

from __future__ import annotations

import argparse
import shutil
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tests.demo_stack import DemoStack  # noqa: E402

OUT = ROOT / "docs" / "screenshots"
APP_PORT = 8790


def capture(stack: DemoStack) -> None:
    from playwright.sync_api import sync_playwright

    OUT.mkdir(parents=True, exist_ok=True)
    url = stack.url
    with sync_playwright() as pw:
        browser = pw.chromium.launch()

        def page_for(width: int, height: int, theme: str = "dark", scale: float = 2):
            ctx = browser.new_context(
                viewport={"width": width, "height": height},
                device_scale_factor=scale,
                color_scheme=theme,
                reduced_motion="no-preference",
            )
            page = ctx.new_page()
            page.goto(url)
            page.wait_for_selector(".conn .dot.ok")
            return page

        def ask(page, text: str, approve: bool = False) -> None:
            page.fill("#composer-input", text)
            page.keyboard.press("Enter")
            if approve:
                page.wait_for_selector(".approval .btn-primary")
                return
            page.wait_for_selector("#send-btn:not(.stop)", timeout=20000)
            page.wait_for_timeout(1700)  # Let the avatar settle and the title arrive.

        # 1. Empty state.
        page = page_for(1440, 900)
        page.wait_for_timeout(600)
        page.screenshot(path=OUT / "empty.png")

        # 2. Conversation with tool use.
        ask(page, "What's the weather in Lisbon this weekend?")
        page.click(".reasoning summary")
        page.wait_for_timeout(300)
        page.screenshot(path=OUT / "chat.png")

        # 3. Approval for a file write.
        ask(page, "Save a note about the trip in my workspace", approve=True)
        page.wait_for_timeout(700)
        page.screenshot(path=OUT / "approval.png")
        page.click(".approval .btn-primary")
        page.wait_for_selector("#send-btn:not(.stop)")

        # 4. Settings.
        page.keyboard.press("Control+,")
        page.click("#tab-model")
        page.wait_for_timeout(400)
        page.screenshot(path=OUT / "settings.png")
        page.context.close()

        # 5. Light theme.
        page = page_for(1440, 900, theme="light")
        page.click(".conv-link >> text=Weekend weather in Lisbon")
        page.wait_for_selector(".tool-card")
        page.mouse.move(900, 860)
        page.wait_for_timeout(500)
        page.screenshot(path=OUT / "light.png")
        page.context.close()

        # 6. Mobile.
        page = page_for(390, 844, scale=3)
        ask(page, "Split €128.68 four ways with a 12% tip")
        page.screenshot(path=OUT / "mobile.png")
        page.context.close()

        # 7. First run without a model server.
        stack.runtime.update_preferences({"base_url": "http://127.0.0.1:9"})
        ctx = browser.new_context(
            viewport={"width": 1440, "height": 900}, device_scale_factor=2, color_scheme="dark"
        )
        page = ctx.new_page()
        page.goto(url + "#/")
        page.wait_for_selector(".setup h3")
        page.wait_for_timeout(1200)
        page.screenshot(path=OUT / "onboarding.png")
        ctx.close()
        browser.close()
    for path in sorted(OUT.glob("*.png")):
        print(f"wrote {path.relative_to(ROOT)} ({path.stat().st_size // 1024} KB)")


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--serve", action="store_true", help="Run the demo stack until Ctrl+C")
    args = parser.parse_args()
    data_dir = Path(tempfile.mkdtemp(prefix="bagley-demo-"))
    try:
        stack = DemoStack(data_dir, delay=0.03 if args.serve else 0.012, app_port=APP_PORT)
        stack.seed()
        if args.serve:
            print(f"Demo running at {stack.url} (Ctrl+C to stop)")
            while True:
                time.sleep(3600)
        capture(stack)
    except KeyboardInterrupt:
        pass
    finally:
        shutil.rmtree(data_dir, ignore_errors=True)


if __name__ == "__main__":
    main()
