"""Runtime configuration, read once from the environment (and .env if present)."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

try:  # optional: load .env for local dev
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:  # pragma: no cover
    pass

ROOT = Path(__file__).resolve().parent.parent


def _bool(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _float(name: str, default: float) -> float:
    raw = os.getenv(name)
    return float(raw) if raw else default


def _configured(role: str) -> str:
    """Model for a voice-agent role from models.yaml (shared with council.py / briefing.py)."""
    try:
        from model_config import load_models

        models = load_models()
        return getattr(models, role, None) or models.main_agent
    except Exception:  # missing file, or yaml/pydantic unavailable
        return "gpt-4.1-mini"


@dataclass(frozen=True)
class Settings:
    # --- ElevenLabs -------------------------------------------------------
    elevenlabs_api_key: str = field(default_factory=lambda: os.getenv("ELEVENLABS_API_KEY", ""))
    elevenlabs_base_url: str = field(
        default_factory=lambda: os.getenv("ELEVENLABS_BASE_URL", "https://api.elevenlabs.io")
    )
    # Default narrator voice; a script can override it per briefing.
    voice_id: str = field(default_factory=lambda: os.getenv("ELEVENLABS_VOICE_ID", "nPczCjzI2devNFxyxzCm"))
    # High-quality model for the scripted narration (prefetched, so latency is hidden).
    tts_model: str = field(default_factory=lambda: os.getenv("ELEVENLABS_TTS_MODEL", "eleven_multilingual_v2"))
    # Low-latency model for live replies (acknowledgements, answers, bridges).
    tts_fast_model: str = field(
        default_factory=lambda: os.getenv("ELEVENLABS_TTS_FAST_MODEL", "eleven_flash_v2_5")
    )
    tts_sample_rate: int = 24000
    stt_model: str = field(
        default_factory=lambda: os.getenv("ELEVENLABS_STT_MODEL", "scribe_v2_realtime")
    )
    stt_batch_model: str = field(
        default_factory=lambda: os.getenv("ELEVENLABS_STT_BATCH_MODEL", "scribe_v2")
    )
    # "realtime" (Scribe websocket, partial transcripts) or "batch" (one POST per utterance)
    stt_mode: str = field(default_factory=lambda: os.getenv("STT_MODE", "realtime"))
    stt_sample_rate: int = 16000

    # --- OpenAI (same provider and model setting as council.py / briefing.py) ---
    openai_enabled: bool = field(default_factory=lambda: bool(os.getenv("OPENAI_API_KEY")))
    # Precedence: ROUTER_MODEL / MAIN_AGENT_MODEL, then OPENAI_MODEL, then models.yaml.
    router_model: str = field(
        default_factory=lambda: os.getenv("ROUTER_MODEL") or os.getenv("OPENAI_MODEL") or _configured("voice_router")
    )
    main_agent_model: str = field(
        default_factory=lambda: os.getenv("MAIN_AGENT_MODEL") or os.getenv("OPENAI_MODEL") or _configured("research_desk")
    )

    # --- News Council databases (written by council.py and briefing.py) ---
    # Synced from the Hugging Face bucket's state/ folder on first run when present (data_store.py).
    council_db: Path = field(default_factory=lambda: Path(os.getenv("COUNCIL_DB", str(ROOT / "council.sqlite3"))))
    preferences_db: Path = field(
        default_factory=lambda: Path(os.getenv("PREFERENCES_DB", str(ROOT / "preferences.sqlite3")))
    )

    # --- Research desk (the voice agent's main agent) ----------------------------------------------------
    # If set, research requests are POSTed here instead of the built-in agent.
    main_agent_url: str = field(default_factory=lambda: os.getenv("MAIN_AGENT_URL", ""))
    main_agent_timeout_s: float = field(default_factory=lambda: _float("MAIN_AGENT_TIMEOUT_S", 60.0))
    # Let the research desk search the web (OpenAI hosted web_search) when the feeds fall short.
    web_search: bool = field(default_factory=lambda: _bool("WEB_SEARCH", True))
    # Origins allowed to call the API from a browser (the GitHub Pages site). Comma-separated, "*" for any.
    cors_origins: str = field(default_factory=lambda: os.getenv("CORS_ORIGINS", "*"))
    # NEWS_DATA_DIR picks one feed directory; by default both liquid-briefing-data/ and news-data/.
    data_dir: Path | None = field(
        default_factory=lambda: Path(os.environ["NEWS_DATA_DIR"]) if os.getenv("NEWS_DATA_DIR") else None
    )

    # --- Conversation feel ---------------------------------------------
    followup_wait_s: float = field(default_factory=lambda: _float("FOLLOWUP_WAIT_S", 7.0))
    hold_line_after_s: float = field(default_factory=lambda: _float("HOLD_LINE_AFTER_S", 5.0))
    # Chunks of audio the server may send ahead of what the listener has heard.
    max_chunks_ahead: int = 2

    # --- Dev ------------------------------------------------------------
    # Generates silent audio of realistic length instead of calling ElevenLabs.
    fake_audio: bool = field(default_factory=lambda: _bool("FAKE_AUDIO"))
    briefings_dir: Path = ROOT / "briefings"
    web_dir: Path = ROOT / "web"
    site_dir: Path = ROOT / "site"
    personas_dir: Path = ROOT / "personas"

    @property
    def elevenlabs_ws_url(self) -> str:
        return self.elevenlabs_base_url.replace("https://", "wss://").replace("http://", "ws://")


def get_settings() -> Settings:
    return Settings()
