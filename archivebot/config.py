from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

INTENTS: dict[str, tuple[str, str]] = {
    # key: (라벨, 이모지)
    "reference": ("레퍼런스", "📚"),
    "competitor": ("경쟁사", "⚔️"),
    "feedback": ("피드백 요청", "💬"),
    "submission": ("제출용", "📤"),
}

DEFAULT_TAGS = "기획,리서치,디자인,개발,발표,회의록,재무,마케팅,기타"
DEFAULT_EXTS = "pdf,pptx,ppt,docx,doc,xlsx,xls,hwp,hwpx,txt,md,csv,key,zip"


def intent_label(intent: str) -> str:
    label, emoji = INTENTS.get(intent, (intent, "📌"))
    return f"{emoji} {label}"


def _csv(value: str) -> list[str]:
    return [v.strip() for v in value.split(",") if v.strip()]


@dataclass
class Config:
    discord_token: str = ""
    anthropic_api_key: str = ""
    model: str = "claude-opus-5-5"
    guild_ids: list[int] = field(default_factory=list)
    data_dir: Path = Path("./data")
    archive_dir: Path = Path("./data/archive")
    archive_base_url: str = ""
    topic_tags: list[str] = field(default_factory=lambda: _csv(DEFAULT_TAGS))
    confidence_threshold: float = 0.75
    context_messages: int = 3
    prompt_ttl: int = 600
    max_file_mb: int = 25
    extensions: set[str] = field(default_factory=lambda: set(_csv(DEFAULT_EXTS)))

    @property
    def db_path(self) -> Path:
        return self.data_dir / "archive.sqlite3"

    @classmethod
    def from_env(cls) -> "Config":
        env = os.environ.get
        data_dir = Path(env("DATA_DIR") or "./data")
        return cls(
            discord_token=env("DISCORD_TOKEN", ""),
            anthropic_api_key=env("ANTHROPIC_API_KEY", ""),
            model=env("ANTHROPIC_MODEL") or "claude-opus-5-5",
            guild_ids=[int(g) for g in _csv(env("GUILD_IDS", ""))],
            data_dir=data_dir,
            archive_dir=Path(env("ARCHIVE_DIR") or data_dir / "archive"),
            archive_base_url=(env("ARCHIVE_BASE_URL") or "").rstrip("/"),
            topic_tags=_csv(env("TOPIC_TAGS") or DEFAULT_TAGS),
            confidence_threshold=float(env("CONFIDENCE_THRESHOLD") or 0.75),
            context_messages=int(env("CONTEXT_MESSAGES") or 3),
            prompt_ttl=int(env("PROMPT_TTL") or 600),
            max_file_mb=int(env("MAX_FILE_MB") or 25),
            extensions={e.lower().lstrip(".") for e in _csv(env("ARCHIVE_EXTENSIONS") or DEFAULT_EXTS)},
        )
