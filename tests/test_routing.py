"""Routing across machines: GPU first, failover, light work kept local, vision models."""

from __future__ import annotations

import httpx
import pytest

from bagley.agent import Agent, RunRequest
from bagley.config import ServerConfig
from bagley.runtime import Runtime
from tests.mock_llm import MODELS, MockLLM, Reply, fake_web_transport

pytestmark = pytest.mark.anyio

GPU_MODELS = {
    **MODELS,
    "qwen3:14b": {
        "capabilities": ["completion", "tools", "thinking"],
        "size": 9_300_000_000,
        "params": "14.8B",
        "family": "qwen3",
    },
    "qwen2.5vl:7b": {
        "capabilities": ["completion", "vision"],
        "size": 6_000_000_000,
        "params": "8.3B",
        "family": "qwen25vl",
    },
}


class HostTransport(httpx.AsyncBaseTransport):
    """Sends each host to its own mock server; hosts in ``down`` refuse connections."""

    def __init__(self, apps: dict[str, MockLLM]) -> None:
        self.apps = apps
        self.down: set[str] = set()

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        host = request.url.host
        if host in self.down or host not in self.apps:
            raise httpx.ConnectError("connection refused", request=request)
        return await httpx.ASGITransport(app=self.apps[host].app).handle_async_request(request)


@pytest.fixture
def machines(tmp_path):
    local, gpu = MockLLM(), MockLLM(models=GPU_MODELS)
    transport = HostTransport({"laptop": local, "h4ch1": gpu})
    rt = Runtime(
        ServerConfig(data_dir=tmp_path / "data", workspace=tmp_path / "ws"),
        env={},
        llm_transport=transport,
        tool_transport=fake_web_transport(),
    )
    rt.store.set_preferences(
        {
            "provider": "ollama",
            "base_url": "http://laptop",
            "model": "qwen3:8b",
            "machine_name": "b1t",
            "smart_titles": False,
            "machines": [
                {
                    "id": "h4ch1",
                    "name": "H4CH1",
                    "role": "gpu",
                    "provider": "ollama",
                    "base_url": "http://h4ch1",
                    "model": "qwen3:14b",
                },
                {
                    "id": "openrouter",
                    "name": "OpenRouter",
                    "role": "cloud",
                    "provider": "openai",
                    "base_url": "http://openrouter/v1",
                    "model": "qwen/qwen3-32b",
                },
            ],
        }
    )
    yield rt, local, gpu, transport
    rt.store.close()


async def test_gpu_machine_answers_when_reachable(machines, recorder):
    rt, local, gpu, _ = machines
    gpu.script = [Reply(text="From the big GPU.")]
    await Agent(rt).run(RunRequest(text="Explain monads"), recorder.emit, recorder.approve)
    assert recorder.text() == "From the big GPU."
    model = recorder.of("model")[0]
    assert model["machine"] == "H4CH1" and model["model"] == "qwen3:14b"
    assert not local.requests
    cid = recorder.of("run.start")[0]["conversation_id"]
    reply = rt.store.list_messages(cid)[-1]
    assert reply["meta"]["machine"] == "H4CH1"
    assert recorder.of("run.end")[0]["stats"]["machine"] == "H4CH1"


async def test_laptop_takes_over_when_the_gpu_is_away(machines, recorder):
    rt, local, _, transport = machines
    transport.down.add("h4ch1")
    local.script = [Reply(text="Small but here.")]
    await Agent(rt).run(RunRequest(text="Hello"), recorder.emit, recorder.approve)
    assert recorder.text() == "Small but here."
    assert recorder.of("model")[0]["machine"] == "B1T"


async def test_failover_mid_run_switches_machine(machines, recorder):
    rt, local, _, transport = machines
    # The probe succeeds, then the GPU disappears before the chat request.
    await rt.router.probe(rt.router.find(rt.preferences()[0], "h4ch1"))
    transport.down.add("h4ch1")
    local.script = [Reply(text="Picked it up.")]
    await Agent(rt).run(RunRequest(text="Hello"), recorder.emit, recorder.approve)
    notices = [n["message"] for n in recorder.of("notice")]
    assert any("H4CH1 is not answering. Switching to B1T." in n for n in notices)
    assert recorder.text() == "Picked it up."
    assert [m["machine"] for m in recorder.of("model")] == ["H4CH1", "B1T"]


async def test_cloud_is_the_last_resort(machines, recorder):
    rt, _, _, transport = machines
    transport.down |= {"h4ch1", "laptop", "openrouter"}
    await Agent(rt).run(RunRequest(text="Hello"), recorder.emit, recorder.approve)
    error = recorder.of("error")[0]
    assert error["message"] == "None of your machines can answer right now."
    assert "H4CH1" in error["hint"] and "OPENROUTER" in error["hint"]


async def test_pinned_routing_and_light_work(machines):
    rt, _, _, _ = machines
    prefs, _ = rt.preferences()
    order = [m.id for m in rt.router.order(prefs)]
    assert order == ["h4ch1", "local", "openrouter"]
    assert rt.router.order(prefs, "light")[0].id == "local"
    rt.update_preferences({"routing": "openrouter"})
    assert [m.id for m in rt.router.order(rt.preferences()[0])] == ["openrouter"]
    rt.update_preferences({"routing": "local"})
    assert [m.id for m in rt.router.order(rt.preferences()[0])] == ["local"]


async def test_titles_stay_on_this_machine(machines, recorder):
    rt, local, gpu, _ = machines
    rt.update_preferences({"smart_titles": True})
    gpu.script = [Reply(text="Answer from the GPU.")]
    agent = Agent(rt)
    await agent.run(
        RunRequest(text="Tell me about weather fronts"), recorder.emit, recorder.approve
    )
    for task in list(agent._background):
        await task
    assert recorder.of("title")
    assert any("Write a title" in r["messages"][-1]["content"] for r in local.requests)
    assert not any("Write a title" in r["messages"][-1]["content"] for r in gpu.requests)


async def test_images_go_to_a_vision_model(machines, recorder):
    rt, _, gpu, _ = machines
    png = b"\x89PNG\r\n\x1a\n" + b"\0" * 64
    (rt.captures_dir / "shot.png").write_bytes(png)
    gpu.script = [Reply(text="That is a segfault.")]
    request = RunRequest(text="What's this error?", images=["shot.png"], source="overlay")
    await Agent(rt).run(request, recorder.emit, recorder.approve)
    assert recorder.of("model")[0]["model"] == "qwen2.5vl:7b"
    sent = gpu.requests[0]["messages"][-1]
    assert sent["images"] and sent["content"].startswith("What's this error?")
    cid = recorder.of("run.start")[0]["conversation_id"]
    user = rt.store.list_messages(cid)[0]
    assert user["meta"] == {"images": ["shot.png"], "source": "overlay"}


async def test_status_reports_every_machine(machines):
    rt, _, _, transport = machines
    transport.down.add("openrouter")
    status = {m["id"]: m for m in await rt.router.status(rt.preferences()[0], fresh=True)}
    assert status["h4ch1"]["ok"] and "qwen3:14b" in status["h4ch1"]["models"]
    assert status["local"]["name"] == "B1T" and status["local"]["priority"] == 1
    assert not status["openrouter"]["ok"] and "api_key" not in status["openrouter"]


async def test_machine_keys_survive_the_browser_round_trip(machines):
    rt, _, _, _ = machines
    machines_list = [m.model_dump() for m in rt.preferences()[0].machines]
    machines_list[1]["api_key"] = "sk-or-secret"
    rt.update_preferences({"machines": machines_list})
    # The browser sends machines back without keys.
    public = [{**m.model_dump(), "api_key": ""} for m in rt.preferences()[0].machines]
    rt.update_preferences({"machines": public})
    assert rt.preferences()[0].machines[1].api_key == "sk-or-secret"
    # Pointing a machine somewhere else drops its key.
    public[1]["base_url"] = "http://elsewhere/v1"
    rt.update_preferences({"machines": public})
    assert rt.preferences()[0].machines[1].api_key == ""
