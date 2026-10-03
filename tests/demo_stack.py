"""Run Bagley plus the mock model server on real ports (for browser tests and screenshots)."""

from __future__ import annotations

import socket
import threading
import time
from pathlib import Path

import uvicorn

from bagley.config import ServerConfig
from bagley.runtime import Runtime
from bagley.server import create_app
from tests.mock_llm import MockLLM, fake_web_transport


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def serve_in_thread(app, port: int) -> uvicorn.Server:
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    threading.Thread(target=server.run, daemon=True).start()
    for _ in range(200):
        with socket.socket() as s:
            if s.connect_ex(("127.0.0.1", port)) == 0:
                return server
        time.sleep(0.05)
    raise RuntimeError(f"Server on port {port} did not start")


class DemoStack:
    def __init__(
        self, data_dir: Path, *, delay: float = 0.01, app_port: int = 0, llm_port: int = 0
    ) -> None:
        self.mock = MockLLM(delay=delay)
        self.llm_port = llm_port or free_port()
        self.app_port = app_port or free_port()
        self.servers = [serve_in_thread(self.mock.app, self.llm_port)]
        self.runtime = Runtime(
            ServerConfig(data_dir=data_dir, port=self.app_port),
            env={},
            tool_transport=fake_web_transport(),
        )
        self.runtime.store.set_preferences(
            {
                "provider": "ollama",
                "base_url": f"http://127.0.0.1:{self.llm_port}",
                "model": "qwen3:8b",
                "think": True,
            }
        )
        self.servers.append(serve_in_thread(create_app(self.runtime), self.app_port))

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.app_port}/"

    def seed(self) -> None:
        store = self.runtime.store
        store.add_memory("Prefers metric units")
        store.add_memory("Lives in Porto, works as a backend developer")
        for title in (
            "Refactor the billing worker",
            "Sourdough starter ratios",
            "Flights to Tokyo in March",
        ):
            store.add_message(store.create_conversation(title)["id"], "user", title)

    def stop(self) -> None:
        for server in self.servers:
            server.should_exit = True
