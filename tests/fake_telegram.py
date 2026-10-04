"""A stand-in for the Telegram Bot API, served through ``httpx.ASGITransport``."""

from __future__ import annotations

import asyncio
import json
import re
import time
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

GOOD_TOKEN = "123456:" + "A" * 35


class FakeTelegram:
    def __init__(self) -> None:
        self.updates: list[dict[str, Any]] = []
        self.sent: list[dict[str, Any]] = []
        self._update_id = 100
        self._message_id = 1
        self.app = FastAPI()

        @self.app.post("/bot{token}/{method}")
        async def method(token: str, method: str, request: Request):
            if token != GOOD_TOKEN:
                return JSONResponse({"ok": False, "description": "Unauthorized"}, status_code=401)
            if request.headers.get("content-type", "").startswith("multipart/"):
                payload = _multipart((await request.body()).decode("utf-8", "replace"))
            else:
                payload = json.loads(await request.body() or b"{}")
            if method == "getMe":
                return {"ok": True, "result": {"id": 1, "username": "bagley_test_bot"}}
            if method == "getUpdates":
                offset = payload.get("offset") or 0
                deadline = time.monotonic() + 0.3
                while True:
                    ready = [u for u in self.updates if u["update_id"] >= offset]
                    if ready or time.monotonic() > deadline:
                        return {"ok": True, "result": ready}
                    await asyncio.sleep(0.02)
            self.sent.append({"method": method, **payload})
            if method in ("sendMessage", "sendPhoto"):
                self._message_id += 1
                return {"ok": True, "result": {"message_id": self._message_id}}
            return {"ok": True, "result": True}

    def _push(self, update: dict[str, Any]) -> None:
        self._update_id += 1
        self.updates.append({"update_id": self._update_id, **update})

    def say(self, chat: int, text: str, name: str = "Ana") -> None:
        self._push(
            {
                "message": {
                    "message_id": self._update_id,
                    "from": {"id": chat, "first_name": name},
                    "chat": {"id": chat, "type": "private"},
                    "text": text,
                }
            }
        )

    def tap(self, chat: int, data: str) -> None:
        self._push(
            {
                "callback_query": {
                    "id": f"cb{self._update_id}",
                    "from": {"id": chat},
                    "message": {"message_id": 1, "chat": {"id": chat}},
                    "data": data,
                }
            }
        )

    def messages(self, chat: int | None = None) -> list[dict[str, Any]]:
        return [
            m
            for m in self.sent
            if m["method"] == "sendMessage" and (chat is None or m["chat_id"] == chat)
        ]

    async def wait_for(self, check, timeout: float = 10.0) -> Any:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            result = check()
            if result:
                return result
            await asyncio.sleep(0.02)
        raise AssertionError(f"Timed out; sent so far: {self.sent[-5:]}")


def _multipart(body: str) -> dict[str, Any]:
    """Enough of a multipart parser for sendPhoto (python-multipart isn't a dependency)."""
    fields: dict[str, Any] = {}
    for m in re.finditer(
        r'name="(\w+)"(?:; filename="([^"]*)")?\r\n(?:[^\r\n]*\r\n)*?\r\n(.*?)\r\n--', body, re.S
    ):
        fields[m.group(1)] = m.group(2) if m.group(2) is not None else m.group(3)
    return fields
