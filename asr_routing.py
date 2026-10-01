"""Which speech recogniser handles which language.

Kept free of heavy imports so the API process can use it too.
"""

import os
import re

# Languages NVIDIA Parakeet TDT v3 was trained on. Russian is listed there as
# well but goes to GigaAM, which is trained specifically for Russian.
PARAKEET_LANGUAGES = frozenset({
    "bg", "hr", "cs", "da", "nl", "en", "et", "fi", "fr", "de", "el", "hu",
    "it", "lv", "lt", "mt", "pl", "pt", "ro", "sk", "sl", "es", "sv", "uk",
})
FAST_ENGINES = ("gigaam", "parakeet")
WHISPER_MODELS = ("small", "medium", "turbo", "large-v3")
DEFAULT_WHISPER_MODEL = "small"


def enabled_fast_engines() -> set[str]:
    raw = os.getenv("ASR_FAST_ENGINES", "gigaam,parakeet")
    return {item.strip().lower() for item in raw.split(",") if item.strip()} & set(FAST_ENGINES)


def normalize_language(language) -> str:
    """Return a lowercase ISO 639-1 style code, or "auto"."""
    value = str(language or "auto").strip().lower()
    return value if re.fullmatch(r"[a-z]{2,3}", value) else "auto"


def normalize_whisper_model(model_size) -> str:
    """tiny/base are no longer offered; old clients fall back to small."""
    value = str(model_size or "").strip().lower()
    return value if value in WHISPER_MODELS else DEFAULT_WHISPER_MODEL


def asr_engine_for_language(language: str) -> str:
    enabled = enabled_fast_engines()
    if language == "ru" and "gigaam" in enabled:
        return "gigaam"
    if language in PARAKEET_LANGUAGES and "parakeet" in enabled:
        return "parakeet"
    return "whisper"
