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

import httpx  # noqa: E402

from bagley.tools import shell  # noqa: E402
from tests.demo_stack import DemoStack  # noqa: E402
from tests.mock_llm import Reply  # noqa: E402

OUT = ROOT / "docs" / "screenshots"
APP_PORT = 8790

CHART_CODE = """import csv
import matplotlib.pyplot as plt

rows = list(csv.DictReader(open("sales.csv")))
months = [r["month"] for r in rows]
revenue = [float(r["revenue"]) for r in rows]
plt.style.use("dark_background")
fig, ax = plt.subplots(figsize=(7, 3), facecolor="#0e0e0e")
ax.set_facecolor("#0e0e0e")
ax.bar(months, revenue, color="#d9d9d9", width=0.6)
ax.set_title("Revenue by month (k EUR)", loc="left", fontsize=11)
ax.spines[["top", "right"]].set_visible(False)
best = max(rows, key=lambda r: float(r["revenue"]))
print(f"total {sum(revenue):.0f}k, best month {best['month']} ({best['revenue']}k)")
"""


def chart(stack: DemoStack, page_for, ask) -> None:
    page = page_for(1440, 900)
    stack.mock.script = [
        Reply(
            reasoning="sales.csv is in the workspace; a bar chart per month answers this.",
            tool_calls=[("run_python", {"code": CHART_CODE})],
        ),
        Reply(
            text="Revenue totals **412k** over six months. June was the best month at "
            "**88k**, and every month after March grew."
        ),
    ]
    ask(page, "Chart my monthly revenue from sales.csv", approve=True)
    page.click(".approval .btn-primary")
    page.wait_for_selector(".tool-media img")
    page.wait_for_selector("#send-btn:not(.stop)")
    page.wait_for_timeout(1700)
    page.screenshot(path=OUT / "chart.png")
    page.context.close()


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
        page.wait_for_selector(".model-row >> text=in memory")
        title = page.locator(".section-title >> text=Installed models")
        title.evaluate("e => e.scrollIntoView({block: 'start'})")
        page.wait_for_timeout(400)
        page.screenshot(path=OUT / "settings.png")

        # 5. Automations.
        sched = stack.runtime.scheduler
        brief = sched.create(
            "task",
            "Morning briefing",
            "weekdays at 08:00",
            "Weather in Lisbon and anything due today in my notes.",
        )
        sched.create("watch", "Laptop price", "every 6 hours", target="https://shop.example.com/x1")
        sched.create("reminder", "Stretch", "in 45 minutes", "Stand up and stretch.")
        stack.mock.script = [Reply(text="Sunny, 24 °C. Q3 plan review is due today.")]
        httpx.post(f"{url}api/automations/{brief['id']}/run")
        page.click("#tab-automations")
        page.wait_for_selector(".automation >> text=Last run")
        page.wait_for_selector(".toast", state="detached", timeout=10000)
        page.screenshot(path=OUT / "automations.png")

        # 6. Knowledge base.
        page.click("#tab-knowledge")
        page.fill(
            "input[aria-label='Search the knowledge base']", "what could go wrong this quarter"
        )
        page.keyboard.press("Enter")
        page.wait_for_selector(".kb-hit")
        page.locator(".kb-hit").last.scroll_into_view_if_needed()
        page.wait_for_timeout(300)
        page.screenshot(path=OUT / "knowledge.png")
        page.keyboard.press("Escape")

        # 7. File edit with a diff.
        stack.mock.script = [
            Reply(
                tool_calls=[
                    (
                        "edit_file",
                        {
                            "path": "notes/q3-plan.md",
                            "find": "Marketing budget is 40k.",
                            "replace": "Marketing budget is 55k after the September review.",
                        },
                    )
                ]
            ),
            Reply(text="Updated the budget line in notes/q3-plan.md."),
        ]
        ask(page, "Bump the Q3 marketing budget to 55k", approve=True)
        page.click(".approval .btn-primary")
        page.wait_for_selector("#send-btn:not(.stop)")
        page.locator(".tool-card .tool-head").last.click()
        page.locator(".diff").last.evaluate("e => e.scrollIntoView({block: 'center'})")
        page.wait_for_timeout(1500)
        page.screenshot(path=OUT / "diff.png")
        page.context.close()

        # 8. Python with a chart (needs matplotlib in the environment Bagley runs).
        try:
            import matplotlib  # noqa: F401
        except ImportError:
            print("matplotlib is not installed: keeping the old chart.png")
        else:
            chart(stack, page_for, ask)

        # 9. Light theme.
        page = page_for(1440, 900, theme="light")
        page.click(".conv-link >> text=Weekend weather in Lisbon")
        page.wait_for_selector(".tool-card")
        page.mouse.move(900, 860)
        page.wait_for_timeout(500)
        page.screenshot(path=OUT / "light.png")
        page.context.close()

        # 10. Mobile.
        page = page_for(390, 844, scale=3)
        ask(page, "Split €128.68 four ways with a 12% tip")
        page.screenshot(path=OUT / "mobile.png")
        page.context.close()

        # 11. First run without a model server.
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
        stack = DemoStack(
            data_dir, delay=0.03 if args.serve else 0.012, app_port=APP_PORT if args.serve else 0
        )
        stack.seed()
        stack.runtime.registry.add_module(shell, "builtin")
        (stack.runtime.config.workspace / "sales.csv").write_text(
            "month,revenue\nJan,58\nFeb,55\nMar,51\nApr,74\nMay,86\nJun,88\n"
        )
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
