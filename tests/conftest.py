from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import httpx
import pytest

from bagley.config import ServerConfig
from bagley.runtime import Runtime
from tests.mock_llm import MockLLM, fake_web_transport


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture
def mock() -> MockLLM:
    return MockLLM()


@pytest.fixture
def config(tmp_path: Path) -> ServerConfig:
    return ServerConfig(data_dir=tmp_path / "data", workspace=tmp_path / "workspace")


@pytest.fixture
def make_runtime(config: ServerConfig, mock: MockLLM):
    created: list[Runtime] = []

    def factory(env: dict[str, str] | None = None, **prefs: Any) -> Runtime:
        rt = Runtime(
            config,
            env=env or {},
            llm_transport=httpx.ASGITransport(app=mock.app),
            tool_transport=fake_web_transport(),
        )
        rt.store.set_preferences(
            {
                "provider": "ollama",
                "base_url": "http://mock",
                "model": "qwen3:8b",
                "smart_titles": False,
                **prefs,
            }
        )
        created.append(rt)
        return rt

    return factory


class Recorder:
    """Collects agent events and answers approvals."""

    def __init__(self, decision: bool = True) -> None:
        self.events: list[dict[str, Any]] = []
        self.decision = decision
        self.asked: list[str] = []

    async def emit(self, event: dict[str, Any]) -> None:
        self.events.append(event)
        await asyncio.sleep(0)  # Yield like a real socket send, so cancellation can land.

    async def approve(self, call, tool) -> bool:
        self.asked.append(tool.name)
        return self.decision

    def types(self) -> list[str]:
        return [e["type"] for e in self.events]

    def of(self, kind: str) -> list[dict[str, Any]]:
        return [e for e in self.events if e["type"] == kind]

    def text(self) -> str:
        return "".join(e["text"] for e in self.of("text.delta"))


@pytest.fixture
def recorder() -> Recorder:
    return Recorder()
