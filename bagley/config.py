"""Configuration.

Two layers:

* ``ServerConfig``: process-level settings (bind address, data paths, security switches).
  Read from the environment and ``.env`` only, never from the browser.
* ``Preferences``: user-tunable settings (model, persona, tools...). Stored in the database
  and editable in the UI. An environment variable for a preference wins over the stored
  value and locks that field in the UI.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field, ValidationError

DEFAULT_PORT = 8765
DEFAULT_OLLAMA_URL = "http://localhost:11434"
LOOPBACK_HOSTS = ("127.0.0.1", "localhost", "::1")


def load_dotenv(path: Path) -> None:
    """Load ``KEY=VALUE`` lines into ``os.environ`` without overriding existing variables."""
    if not path.is_file():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip().removeprefix("export ").strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        elif " #" in value:
            value = value.split(" #", 1)[0].rstrip()
        os.environ.setdefault(key, value)


def _bool(value: str | None, default: bool = False) -> bool:
    if value is None or value.strip() == "":
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def normalize_base_url(url: str) -> str:
    """Accept ``host``, ``host:port`` or a full URL (the forms ``OLLAMA_HOST`` allows)."""
    url = url.strip().rstrip("/")
    if not url:
        return DEFAULT_OLLAMA_URL
    bare = "://" not in url
    if bare:
        url = "http://" + url
    scheme, _, rest = url.partition("://")
    host, slash, path = rest.partition("/")
    if host.startswith("0.0.0.0"):
        host = "127.0.0.1" + host[len("0.0.0.0") :]
    if bare and ":" not in host.rsplit("]", 1)[-1]:
        host += ":11434"
    return f"{scheme}://{host}{slash}{path}".rstrip("/")


@dataclass
class ServerConfig:
    host: str = "127.0.0.1"
    port: int = DEFAULT_PORT
    data_dir: Path = field(default_factory=lambda: Path.home() / ".bagley")
    workspace: Path | None = None
    plugins_dir: Path | None = None
    mcp_config: Path | None = None
    enable_shell: bool = False
    python: str = ""
    allow_private_urls: bool = False
    searxng_url: str = ""
    token: str = ""
    allowed_hosts: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.data_dir = Path(self.data_dir).expanduser()
        self.workspace = Path(self.workspace or self.data_dir / "workspace").expanduser()
        self.plugins_dir = Path(self.plugins_dir or self.data_dir / "plugins").expanduser()
        self.mcp_config = Path(self.mcp_config or self.data_dir / "mcp.json").expanduser()

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> ServerConfig:
        env = os.environ if env is None else env
        data_dir = env.get("BAGLEY_DATA_DIR") or str(Path.home() / ".bagley")
        try:
            port = int(env.get("BAGLEY_PORT") or DEFAULT_PORT)
        except ValueError as exc:
            raise SystemExit(f"BAGLEY_PORT must be a number, got {env['BAGLEY_PORT']!r}") from exc
        hosts = [h.strip() for h in env.get("BAGLEY_ALLOWED_HOSTS", "").split(",") if h.strip()]
        return cls(
            host=env.get("BAGLEY_HOST") or "127.0.0.1",
            port=port,
            data_dir=Path(data_dir),
            workspace=Path(env["BAGLEY_WORKSPACE"]) if env.get("BAGLEY_WORKSPACE") else None,
            plugins_dir=Path(env["BAGLEY_PLUGINS_DIR"]) if env.get("BAGLEY_PLUGINS_DIR") else None,
            mcp_config=Path(env["BAGLEY_MCP_CONFIG"]) if env.get("BAGLEY_MCP_CONFIG") else None,
            enable_shell=_bool(env.get("BAGLEY_ENABLE_SHELL")),
            python=env.get("BAGLEY_PYTHON") or "",
            allow_private_urls=_bool(env.get("BAGLEY_ALLOW_PRIVATE_URLS")),
            searxng_url=(env.get("BAGLEY_SEARXNG_URL") or "").rstrip("/"),
            token=env.get("BAGLEY_TOKEN") or "",
            allowed_hosts=hosts,
        )

    @property
    def db_path(self) -> Path:
        return self.data_dir / "bagley.db"

    @property
    def is_loopback(self) -> bool:
        return self.host in LOOPBACK_HOSTS

    def ensure_dirs(self) -> None:
        for path in (self.data_dir, self.workspace, self.plugins_dir):
            assert path is not None
            path.mkdir(parents=True, exist_ok=True)


Persona = Literal["bagley", "professional", "concise"]


class Preferences(BaseModel):
    provider: Literal["auto", "ollama", "openai"] = "auto"
    base_url: str = DEFAULT_OLLAMA_URL
    api_key: str = ""
    model: str = ""
    temperature: float = Field(0.6, ge=0, le=2)
    context_tokens: int = Field(8192, ge=1024, le=1_048_576)
    max_steps: int = Field(8, ge=1, le=32)
    tool_mode: Literal["auto", "native", "prompt", "off"] = "auto"
    tool_routing: bool = True
    think: bool = False
    persona: Persona = "bagley"
    custom_instructions: str = Field("", max_length=4000)
    disabled_tools: list[str] = Field(default_factory=list)
    smart_titles: bool = True
    knowledge_folders: list[str] = Field(default_factory=list)
    embedding_model: str = ""  # "" picks an installed embedding model, "off" disables.


PREFERENCE_ENV: dict[str, str] = {
    "provider": "BAGLEY_PROVIDER",
    "base_url": "BAGLEY_BASE_URL",
    "api_key": "BAGLEY_API_KEY",
    "model": "BAGLEY_MODEL",
    "temperature": "BAGLEY_TEMPERATURE",
    "context_tokens": "BAGLEY_CONTEXT_TOKENS",
    "max_steps": "BAGLEY_MAX_STEPS",
    "tool_mode": "BAGLEY_TOOL_MODE",
    "think": "BAGLEY_THINK",
    "persona": "BAGLEY_PERSONA",
}

# Preferences that the browser can read but never see in full.
SECRET_PREFERENCES = {"api_key"}


def env_preferences(env: Mapping[str, str] | None = None) -> dict[str, Any]:
    env = os.environ if env is None else env
    values: dict[str, Any] = {}
    for key, var in PREFERENCE_ENV.items():
        raw = env.get(var)
        if raw is None or raw.strip() == "":
            continue
        values[key] = _bool(raw) if key == "think" else raw.strip()
    if "base_url" in values:
        values["base_url"] = normalize_base_url(values["base_url"])
    return values


def resolve_preferences(
    stored: Mapping[str, Any], env: Mapping[str, str] | None = None
) -> tuple[Preferences, set[str]]:
    """Merge defaults, stored values and environment overrides.

    Returns the effective preferences and the set of keys locked by the environment.
    Invalid stored values are dropped individually instead of failing the whole merge.
    """
    env = os.environ if env is None else env
    merged: dict[str, Any] = {}
    if env.get("OLLAMA_HOST") and "base_url" not in stored:
        merged["base_url"] = normalize_base_url(env["OLLAMA_HOST"])
    for key, value in stored.items():
        if key not in Preferences.model_fields:
            continue
        try:
            Preferences.model_validate({**merged, key: value})
        except ValidationError:
            continue
        merged[key] = value
    overrides = env_preferences(env)
    try:
        prefs = Preferences.model_validate({**merged, **overrides})
    except ValidationError as exc:
        names = ", ".join(PREFERENCE_ENV[str(e["loc"][0])] for e in exc.errors() if e["loc"])
        raise SystemExit(f"Invalid environment configuration: {names}\n{exc}") from exc
    locked = set(overrides)
    if "api_key" in locked:
        # A key from the environment must only ever go to the server it was configured for.
        locked |= {"provider", "base_url"}
    return prefs, locked
