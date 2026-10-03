"""Browser tests: drive the real UI against the mock model server.

Skipped unless Playwright and a Chromium build are available
(``pip install playwright && playwright install chromium``).
"""

from __future__ import annotations

import pytest

from tests.demo_stack import DemoStack
from tests.mock_llm import Reply

sync_api = pytest.importorskip("playwright.sync_api")

pytestmark = pytest.mark.ui


@pytest.fixture(scope="module")
def browser():
    with sync_api.sync_playwright() as pw:
        try:
            browser = pw.chromium.launch()
        except Exception as exc:  # Browser binaries missing.
            pytest.skip(f"Chromium unavailable: {exc}")
        yield browser
        browser.close()


@pytest.fixture
def stack(tmp_path):
    s = DemoStack(tmp_path / "data")
    yield s
    s.stop()


@pytest.fixture
def page(browser, stack):
    context = browser.new_context(viewport={"width": 1366, "height": 860})
    page = context.new_page()
    errors: list[str] = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.goto(stack.url)
    page.wait_for_selector(".conn .dot.ok")
    yield page
    context.close()
    assert errors == [], errors


def send(page, text: str) -> None:
    page.fill("#composer-input", text)
    page.keyboard.press("Enter")


def wait_idle(page) -> None:
    page.wait_for_selector("#send-btn:not(.stop)", timeout=15000)


def test_chat_with_tool_call_and_reload(page, stack):
    send(page, "What's the weather in Lisbon this weekend?")
    wait_idle(page)
    assert page.locator('.tool-card[data-state="ok"]').count() == 1
    assert "22°C and mostly clear" in page.inner_text(".turn-assistant .prose")
    page.wait_for_function(
        "document.querySelector('#title-btn').textContent === 'Weekend weather in Lisbon'"
    )
    assert page.locator(".conv-item.active").inner_text().startswith("Weekend weather")

    page.reload()
    page.wait_for_selector('.tool-card[data-state="ok"]')
    assert page.locator(".turn").count() == 2
    page.click(".tool-card .tool-head")
    assert "Lisbon" in page.inner_text(".tool-card .tool-detail")


def test_approval_deny_then_regenerate_and_allow(page, stack):
    send(page, "Save a note about the trip")
    page.click(".approval >> text=Deny")
    wait_idle(page)
    assert page.locator('.tool-card[data-state="denied"]').count() == 1
    assert not (stack.runtime.config.workspace / "trip" / "lisbon.md").exists()

    page.click(".turn-assistant.last [aria-label='Regenerate']")
    page.click(".approval .btn-primary")
    wait_idle(page)
    assert (stack.runtime.config.workspace / "trip" / "lisbon.md").exists()
    assert page.locator(".turn-assistant").count() == 1


def test_edit_last_message(page, stack):
    send(page, "Hello")
    wait_idle(page)
    stack.mock.script = [Reply(text="Edited reply.")]
    page.focus("#composer-input")
    page.keyboard.press("ArrowUp")
    page.fill(".bubble-edit textarea", "Hello again")
    page.click("text=Save & send")
    wait_idle(page)
    assert page.inner_text(".turn-user .bubble") == "Hello again"
    assert "Edited reply." in page.inner_text(".turn-assistant .prose")


def test_stop_keeps_partial_reply(page, stack):
    stack.mock.delay = 0.05
    stack.mock.script = [Reply(text="word " * 300)]
    send(page, "Talk for a while")
    page.wait_for_selector(".prose.streaming")
    page.keyboard.press("Escape")
    wait_idle(page)
    cid = stack.runtime.store.list_conversations()[0]["id"]
    last = stack.runtime.store.list_messages(cid)[-1]
    assert last["meta"].get("interrupted") is True


def test_sidebar_search_rename_delete_undo(page, stack):
    stack.seed()
    page.reload()
    page.wait_for_selector(".conv-item")
    page.fill("#search", "sourdough")
    page.wait_for_function("document.querySelectorAll('.conv-item').length === 1")

    page.hover(".conv-item")
    page.click(".conv-item [aria-label^='Rename']")
    page.fill(".conv-rename", "Bread notes")
    page.keyboard.press("Enter")
    page.wait_for_selector(".conv-link >> text=Bread notes")

    page.fill("#search", "")
    page.wait_for_function("document.querySelectorAll('.conv-item').length === 3")
    page.hover(".conv-item >> nth=0")
    page.click(".conv-item >> nth=0 >> [aria-label^='Delete']")
    page.wait_for_function("document.querySelectorAll('.conv-item').length === 2")
    page.click(".toast >> text=Undo")
    page.wait_for_function("document.querySelectorAll('.conv-item').length === 3")


def test_settings_persona_memory_and_theme(page, stack):
    page.keyboard.press("Control+,")
    page.click(".choice >> text=Concise")
    page.wait_for_selector("#save-state >> text=Saved")
    assert stack.runtime.preferences()[0].persona == "concise"

    page.click("#tab-memory")
    page.fill("input[aria-label='New memory']", "Allergic to peanuts")
    page.keyboard.press("Enter")
    page.wait_for_selector(".list-item >> text=Allergic to peanuts")
    assert stack.runtime.store.list_memories()[0]["content"] == "Allergic to peanuts"

    page.click("#tab-appearance")
    page.click(".segmented >> text=Light")
    assert page.evaluate("document.documentElement.dataset.theme") == "light"
    page.keyboard.press("Escape")
    page.reload()
    assert page.evaluate("document.documentElement.dataset.theme") == "light"


def test_mobile_layout(browser, stack):
    context = browser.new_context(
        viewport={"width": 390, "height": 844}, has_touch=True, is_mobile=True
    )
    page = context.new_page()
    page.goto(stack.url)
    page.wait_for_selector(".conn .dot.ok", state="attached")
    assert page.is_visible(".topbar-avatar")
    assert not page.is_visible(".presence")
    send(page, "Split €128.68 four ways with a 12% tip")
    wait_idle(page)
    assert "€41.18" in page.inner_text(".turn-assistant .prose")
    page.click("#sidebar-open")
    page.wait_for_selector(".app.sidebar-open")
    page.click("#scrim", position={"x": 370, "y": 400})
    page.wait_for_selector(".app:not(.sidebar-open)")
    context.close()
