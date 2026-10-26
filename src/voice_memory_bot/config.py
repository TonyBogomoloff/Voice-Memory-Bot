from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit


@dataclass(frozen=True)
class LLMConfig:
    provider: str = "codex"
    model: str = "gpt-5.6-luna"
    effort: str = "low"
    binary: str = "codex"
    timeout_seconds: int = 60


@dataclass(frozen=True)
class Limits:
    max_file_bytes: int = 20_000_000
    max_audio_seconds: int = 1800
    max_pending_per_user: int = 20
    max_user_storage_bytes: int = 1_000_000_000
    max_notes_per_user: int = 10000
    max_folders_per_user: int = 100
    daily_asr_seconds: int = 7200
    export_part_bytes: int = 45_000_000
    max_concurrent_owners: int = 4


@dataclass(frozen=True)
class Config:
    bot_token: str = field(repr=False)
    allowed_user_ids: frozenset[int]
    groq_voice: str = field(repr=False)
    data_dir: Path
    groq_model: str = "whisper-large-v3-turbo"
    llm: LLMConfig = field(default_factory=LLMConfig)
    limits: Limits = field(default_factory=Limits)
    assistant_models: tuple[str, ...] = ()
    folder_name_model: str = ""
    telegram_api_base: str = "https://api.telegram.org"
    telegram_file_root: Path | None = None
    obsidian_url: str = ""
    obsidian_port: int = 8788

    @classmethod
    def load(cls, path: Path, secrets_path: Path | None = None) -> Config:
        raw = json.loads(path.read_text(encoding="utf-8"))
        secrets = json.loads(secrets_path.read_text(encoding="utf-8")) if secrets_path else raw
        ids = raw.get("allowed_user_ids", [])
        if not ids or any(type(i) is not int or i <= 0 for i in ids):
            raise ValueError("allowed_user_ids: укажи Telegram ID разрешённых пользователей")
        token = secrets.get("bot_token_your_audio_notes") or raw.get("bot_token", "")
        if not isinstance(token, str) or ":" not in token or any(c.isspace() for c in token):
            raise ValueError("bot_token_your_audio_notes: нужен токен нового бота")
        key = secrets.get("groq_voice") or raw.get("groq_voice", "")
        if not isinstance(key, str) or not key.strip():
            raise ValueError("groq_voice: нужен ключ Groq")
        llm = LLMConfig(**raw.get("llm", {}))
        if llm.provider != "codex" or llm.effort not in {"low", "medium", "high"}:
            raise ValueError("MVP поддерживает llm.provider=codex и effort=low/medium/high")
        if not 5 <= llm.timeout_seconds <= 180:
            raise ValueError("llm.timeout_seconds: допустимо 5–180")
        models = raw.get("assistant_models", [])
        if (not isinstance(models, list) or len(models) > 10
                or any(not isinstance(m, str) or not 1 <= len(m) <= 100
                       or not all(c.isalnum() or c in "-._" for c in m) for m in models)):
            raise ValueError("assistant_models: укажи до 10 идентификаторов доступных моделей")
        name_model = raw.get("folder_name_model", "")
        if not isinstance(name_model, str) or len(name_model) > 100 or any(
                not (c.isalnum() or c in "-._") for c in name_model):
            raise ValueError("folder_name_model: нужен идентификатор модели")
        limits = Limits(**raw.get("limits", {}))
        if any(type(v) is not int or v <= 0 for v in vars(limits).values()):
            raise ValueError("Все limits должны быть положительными целыми числами")
        api_base = os.environ.get("VOICE_MEMORY_TELEGRAM_API_BASE", raw.get("telegram_api_base", "https://api.telegram.org")).rstrip("/")
        api = urlsplit(api_base)
        local_api = api_base != "https://api.telegram.org"
        if (api.scheme not in {"http", "https"} or not api.hostname or api.username or api.password
                or api.path or api.query or api.fragment
                or local_api and api.hostname not in {"localhost", "127.0.0.1", "::1"}):
            raise ValueError("telegram_api_base: нужен облачный API или локальный адрес loopback")
        file_root = os.environ.get("VOICE_MEMORY_TELEGRAM_FILE_ROOT", raw.get("telegram_file_root", ""))
        if local_api and (not file_root or not Path(file_root).is_absolute()):
            raise ValueError("telegram_file_root: нужен абсолютный каталог локального Bot API")
        if local_api:
            if limits.max_file_bytes > 1_900_000_000 or not 21_000_000 <= limits.export_part_bytes <= 1_950_000_000:
                raise ValueError("Локальный Bot API: файл до 1900 MB, часть экспорта 21–1950 MB")
        elif limits.max_file_bytes > 20_000_000 or not 21_000_000 <= limits.export_part_bytes <= 45_000_000:
            raise ValueError("Для облачного Bot API: файл до 20 MB, часть экспорта 21–45 MB")
        obsidian_url = os.environ.get("VOICE_MEMORY_OBSIDIAN_URL", raw.get("obsidian_url", "")).rstrip("/")
        endpoint = urlsplit(obsidian_url)
        if obsidian_url and (endpoint.scheme != "https" or not endpoint.hostname or endpoint.username
                             or endpoint.password or endpoint.path or endpoint.query or endpoint.fragment):
            raise ValueError("obsidian_url: нужен HTTPS-адрес без пути и пароля")
        obsidian_port = int(os.environ.get("VOICE_MEMORY_OBSIDIAN_PORT", raw.get("obsidian_port", 8788)))
        if not 1024 <= obsidian_port <= 65535:
            raise ValueError("obsidian_port: допустимо 1024–65535")
        directory = Path(raw.get("data_dir", "data"))
        if not directory.is_absolute():
            directory = path.resolve().parent / directory
        return cls(token, frozenset(ids), key, directory.resolve(),
                   raw.get("groq_model", "whisper-large-v3-turbo"), llm, limits, tuple(models),
                   name_model, api_base, Path(file_root).resolve() if local_api else None,
                   obsidian_url, obsidian_port)
