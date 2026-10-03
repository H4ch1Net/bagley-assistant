"""Browser tests: drive the real UI against the mock model server.

Skipped unless Playwright and a Chromium build are available
(``pip install playwright && playwright install chromium``).
"""

from __future__ import annotations

import re

import pytest

from tests.demo_stack import DemoStack
from tests.mock_llm import Reply

sync_api = pytest.importorskip("playwright.sync_api")
expect = sync_api.expect

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
    expect(page.locator("#title-btn")).to_have_text("Weekend weather in Lisbon")
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
    expect(page.locator(".conv-item")).to_have_count(1)

    page.hover(".conv-item")
    page.click(".conv-item [aria-label^='Rename']")
    page.fill(".conv-rename", "Bread notes")
    page.keyboard.press("Enter")
    page.wait_for_selector(".conv-link >> text=Bread notes")

    page.fill("#search", "")
    expect(page.locator(".conv-item")).to_have_count(3)
    page.hover(".conv-item >> nth=0")
    page.click(".conv-item >> nth=0 >> [aria-label^='Delete']")
    expect(page.locator(".conv-item")).to_have_count(2)
    page.click(".toast >> text=Undo")
    expect(page.locator(".conv-item")).to_have_count(3)


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
    expect(page.locator("html")).to_have_attribute("data-theme", "light")
    page.keyboard.press("Escape")
    page.reload()
    expect(page.locator("html")).to_have_attribute("data-theme", "light")


def test_attach_file(page, stack, tmp_path):
    note = tmp_path / "todo.txt"
    note.write_text("buy milk")
    page.set_input_files("#file-input", str(note))
    page.wait_for_selector(".chip.ready")
    page.click("#send-btn")
    wait_idle(page)
    assert "Attached: uploads/todo.txt" in page.inner_text(".turn-user .bubble")
    assert (stack.runtime.config.workspace / "uploads" / "todo.txt").read_text() == "buy milk"
    assert page.locator(".chip").count() == 0


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


def test_model_output_cannot_load_images_or_shadow_app_ids(page, stack):
    stack.mock.script = [
        Reply(
            text='Look ![pixel](http://127.0.0.1:9/leak?d=secret) <img src="http://127.0.0.1:9/x"> '
            '<div id="toasts">fake</div>\n\n```python\nprint("hi")\n```'
        )
    ]
    send(page, "hi")
    wait_idle(page)
    thread = page.locator("#messages")
    assert thread.locator("img").count() == 0
    assert thread.locator("#toasts").count() == 0
    assert thread.locator("a >> text=Image: pixel").count() == 1
    copy = thread.locator("button[data-copy-code]")
    assert copy.locator("use").count() == 1
    copy.click()  # Must not throw.


def test_first_reply_survives_switching_chats(page, stack):
    stack.seed()
    page.reload()
    page.wait_for_selector(".conv-item")
    stack.mock.delay = 0.03
    stack.mock.script = [Reply(text="word " * 150)]
    send(page, "Tell me a long story")
    page.wait_for_selector(".turn-user:not(.pending)")
    page.wait_for_selector(".prose.streaming")
    page.click(".conv-link >> text=Sourdough starter ratios")
    page.go_back()
    wait_idle(page)
    assert page.inner_text(".turn-assistant .prose").startswith("word word")


def test_settings_keep_focus_while_saving(page, stack):
    page.keyboard.press("Control+,")
    box = page.locator("#custom-instructions")
    box.click()
    page.keyboard.type("Call me Sam.")
    page.wait_for_selector("#save-state >> text=Saved")
    page.keyboard.type(" I live in Leeds.")
    expect(box).to_have_value("Call me Sam. I live in Leeds.")
    expect(box).to_be_focused()


def test_file_change_shows_diff_and_reverts(page, stack):
    note = stack.runtime.config.workspace / "todo.md"
    note.write_text("- milk\n- eggs\n")
    stack.mock.script = [
        Reply(
            tool_calls=[
                ("edit_file", {"path": "todo.md", "find": "- eggs", "replace": "- eggs\n- bread"})
            ]
        ),
        Reply(text="Added bread."),
    ]
    send(page, "Add bread to my todo list")
    page.click(".approval .btn-primary")
    wait_idle(page)
    expect(page.locator(".diffstat .add")).to_have_text("+1")
    assert note.read_text() == "- milk\n- eggs\n- bread\n"
    page.click(".tool-card .tool-head")
    expect(page.locator(".diff .add")).to_have_text("+- bread")
    page.click(".tool-bar .revert")
    expect(page.locator(".tool-bar .badge")).to_have_text("Reverted")
    assert note.read_text() == "- milk\n- eggs\n"
    page.reload()
    expect(page.locator(".tool-bar .badge")).to_have_text("Reverted")


def test_create_and_run_a_reminder(page, stack):
    page.click("#automations-btn")
    page.click(".segmented >> text=Reminder")
    page.fill("input[placeholder='e.g. Stretch']", "Stretch")
    page.fill("#settings-panel textarea", "Stand up and stretch")
    page.fill("input[list='schedule-presets']", "in 45 minutes")
    expect(page.locator(".field .help").filter(has_text="Once,")).to_be_visible()
    page.click("button:has-text('Create')")
    row = page.locator(".list-item.automation").filter(has_text="Stretch")
    expect(row).to_contain_text(re.compile(r"next in 4[45] min"))
    expect(page.locator(".toast")).to_have_count(0)
    row.locator("[aria-label='Run Stretch now']").click()
    page.locator(".toast >> text=Open").click()
    expect(page.locator(".turn-assistant .prose")).to_contain_text("Reminder: Stand up and stretch")
    expect(page.locator(".stat").filter(has_text="Automations")).to_contain_text("1 · in 4")


def test_python_chart_shows_inline(page, stack):
    pytest.importorskip("matplotlib")
    from bagley.tools import shell

    stack.runtime.registry.add_module(shell, "builtin")
    code = "import matplotlib.pyplot as plt\nplt.bar(['a', 'b'], [3, 5])"
    stack.mock.script = [Reply(tool_calls=[("run_python", {"code": code})]), Reply(text="Done.")]
    send(page, "chart a and b")
    page.click(".approval .btn-primary")
    image = page.locator(".tool-media img")
    expect(image).to_have_count(1)
    assert image.evaluate("img => img.decode().then(() => img.naturalWidth)") > 100


def test_model_manager_unloads(page, stack):
    page.keyboard.press("Control+,")
    page.click("#tab-model")
    row = page.locator(".model-row[data-name='qwen3:8b']")
    expect(row).to_contain_text("in memory")
    row.locator("button:has-text('Unload')").click()
    expect(row).not_to_contain_text("in memory")
    expect(page.locator(".model-row")).to_have_count(len(stack.mock.models))


def test_automation_form_keeps_draft_labels_and_toasts(page, stack):
    page.click("#automations-btn")
    page.get_by_label("Name").fill("Laptop price")
    page.get_by_label("Instructions").fill("Tell me if it drops below 800")
    page.click(".segmented >> text=Watch page")
    expect(page.get_by_label("Name")).to_have_value("Laptop price")
    expect(page.get_by_label("When it changes")).to_have_value("Tell me if it drops below 800")
    page.get_by_label("Page URL").fill("https://shop.example.com/x1")
    expect(page.get_by_label("When", exact=True)).to_have_value("every 1 hour")
    page.click("button:has-text('Create')")
    toast = page.locator(".toast >> text=Watching the page")
    expect(toast).to_be_visible()
    page.click("#tab-knowledge")  # Redrawing the dialog must not swallow the toast.
    expect(toast).to_be_visible()
    page.click("#tab-automations")
    expect(page.get_by_label("Name")).to_have_value("")
