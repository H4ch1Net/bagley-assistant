"""Hosted model APIs Bagley knows how to set up: where they live, how their keys are named.

Each becomes a "cloud" machine. Claude goes through the Anthropic SDK; the others speak the
OpenAI-compatible API. Keys are saved with the machine or read from ``key_env``.
"""

from __future__ import annotations

from typing import Any

PRESETS: dict[str, dict[str, Any]] = {
    "claude": {
        "name": "Claude",
        "label": "Claude (Anthropic)",
        "provider": "anthropic",
        "base_url": "https://api.anthropic.com",
        "model": "claude-opus-5-5",
        "vision_model": "",
        "key_env": "ANTHROPIC_API_KEY",
        "keys_url": "https://console.anthropic.com/settings/keys",
        "models": ["claude-opus-5-5", "claude-sonnet-5-5", "claude-haiku-5-5", "claude-fable-5-1"],
    },
    "openai": {
        "name": "OpenAI",
        "label": "OpenAI",
        "provider": "openai",
        "base_url": "https://api.openai.com/v1",
        "key_env": "OPENAI_API_KEY",
        "keys_url": "https://platform.openai.com/api-keys",
    },
    "openrouter": {
        "name": "OpenRouter",
        "label": "OpenRouter",
        "provider": "openai",
        "base_url": "https://openrouter.ai/api/v1",
        "key_env": "OPENROUTER_API_KEY",
        "keys_url": "https://openrouter.ai/settings/keys",
    },
    "groq": {
        "name": "Groq",
        "label": "Groq",
        "provider": "openai",
        "base_url": "https://api.groq.com/openai/v1",
        "key_env": "GROQ_API_KEY",
        "keys_url": "https://console.groq.com/keys",
    },
    "mistral": {
        "name": "Mistral",
        "label": "Mistral",
        "provider": "openai",
        "base_url": "https://api.mistral.ai/v1",
        "key_env": "MISTRAL_API_KEY",
        "keys_url": "https://console.mistral.ai/api-keys",
    },
    "gemini": {
        "name": "Gemini",
        "label": "Google Gemini",
        "provider": "openai",
        "base_url": "https://generativelanguage.googleapis.com/v1beta/openai",
        "key_env": "GEMINI_API_KEY",
        "keys_url": "https://aistudio.google.com/apikey",
    },
    "deepseek": {
        "name": "DeepSeek",
        "label": "DeepSeek",
        "provider": "openai",
        "base_url": "https://api.deepseek.com/v1",
        "key_env": "DEEPSEEK_API_KEY",
        "keys_url": "https://platform.deepseek.com/api_keys",
    },
    "xai": {
        "name": "xAI",
        "label": "xAI (Grok)",
        "provider": "openai",
        "base_url": "https://api.x.ai/v1",
        "key_env": "XAI_API_KEY",
        "keys_url": "https://console.x.ai",
    },
}


def public(env: dict[str, str] | Any) -> list[dict[str, Any]]:
    """The presets for the settings UI, with whether each key is already in the environment."""
    return [
        {
            "id": pid,
            "model": "",
            "models": [],
            **preset,
            "key_in_env": bool(env.get(preset["key_env"], "")),
        }
        for pid, preset in PRESETS.items()
    ]
