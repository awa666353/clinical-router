"""Runtime configuration for the clinical learning-to-defer demo."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:  # pragma: no cover - optional convenience for minimal installs
    pass


BASE_DIR = Path(__file__).resolve().parent


@dataclass(frozen=True)
class Settings:
    deepseek_api_key: str = os.getenv("DEEPSEEK_API_KEY", "")
    deepseek_base_url: str = os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com")
    deepseek_model: str = os.getenv("DEEPSEEK_MODEL", "deepseek-chat")
    deepseek_timeout_seconds: float = float(os.getenv("DEEPSEEK_TIMEOUT_SECONDS", "20"))
    deepseek_temperature: float = float(os.getenv("DEEPSEEK_TEMPERATURE", "0.2"))
    deepseek_max_tokens: int = int(os.getenv("DEEPSEEK_MAX_TOKENS", "160"))
    initial_llm_budget: int = int(os.getenv("INITIAL_LLM_BUDGET", "4"))
    initial_human_budget: int = int(os.getenv("INITIAL_HUMAN_BUDGET", "1"))
    max_turns: int = int(os.getenv("MAX_TURNS", "12"))
    cases_path: Path = BASE_DIR / "cases" / "chest_pain_cases.json"
    trajectory_path: Path = BASE_DIR / "logs" / "trajectory.jsonl"


SETTINGS = Settings()
