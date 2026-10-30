"""
Configuration helpers for the Clauver MCP bridge.
"""

from __future__ import annotations

import os


DEFAULT_AGENT_NAME = "clauver-general"
# Default verified against OpenAI model docs and LiveKit OpenAI plugin list:
# https://developers.openai.com/api/docs/models/gpt-5.4-mini
# https://docs.livekit.io/agents/models/llm/openai/
DEFAULT_LLM_MODEL = "gpt-5.4-mini"


def get_required_env(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise RuntimeError(f"Missing required env var: {name}")
    return value


def ensure_livekit_env() -> None:
    get_required_env("LIVEKIT_URL")
    get_required_env("LIVEKIT_API_KEY")
    get_required_env("LIVEKIT_API_SECRET")


def ensure_worker_env() -> None:
    ensure_livekit_env()
    get_required_env("SIP_OUTBOUND_TRUNK_ID")
    get_required_env("OPENAI_API_KEY")
    get_required_env("DEEPGRAM_API_KEY")
    get_required_env("CARTESIA_API_KEY")


def get_agent_name() -> str:
    return os.getenv("CLAUVER_AGENT_NAME", DEFAULT_AGENT_NAME)


def get_boss_name() -> str:
    return os.getenv("BOSS_NAME", "Boss")


def get_llm_model() -> str:
    if "CLAUVER_LLM_MODEL" not in os.environ:
        return DEFAULT_LLM_MODEL
    model = os.environ["CLAUVER_LLM_MODEL"].strip()
    if not model:
        raise RuntimeError("CLAUVER_LLM_MODEL is set but empty")
    return model