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
DEFAULT_EXTS = "pdf,pptx,ppt,docx,doc,xlsx,xls,hwp,hwpx,txt,md,csv,key,zip,png,jpg,jpeg,webp"


TAG_GUIDE = {
    "기획": "기획안·일정·역할 분담·아이디어 정리",
    "리서치": "조사 자료·논문·기사·통계·인터뷰 결과",
    "디자인": "시안·UI·로고·이미지 작업물",
    "개발": "코드·기술 문서·API·설계",
    "발표": "발표 자료·대본·포스터",
    "회의록": "회의 기록·회의 메모",
    "재무": "예산·정산·견적·매출",
    "마케팅": "홍보·SNS·광고·고객 반응",
    "기타": "어디에도 딱 맞지 않을 때만",
}


def default_rules(topic_tags: list[str]) -> list[str]:
    """서버에서 처음 쓸 때 미리 넣어 두는 규칙. 팀이 /규칙 목록에서 보고 고치거나 지울 수 있다."""
    guide = " / ".join(f"{t}: {TAG_GUIDE[t]}" if t in TAG_GUIDE else t for t in topic_tags)
    return [
        f"주제 태그는 1~2개만 붙인다. 기준 — {guide}",
        "공유 의도 기준 — 제출용: 교수님·심사위원·고객 등 밖으로 낼 최종본 / 피드백 요청: 팀원 의견을 구하는 우리 팀 "
        "초안·수정본 / 경쟁사: 다른 팀·회사·제품을 분석하거나 경계하려는 자료 / 레퍼런스: 참고하라고 공유한 외부 자료·사례",
        "올린 사람이 의도나 주제를 말로 밝히면(예: '참고해', '피드백 부탁', '이거 제출본') 그 말을 가장 우선한다.",
    ]


def intent_label(intent: str) -> str:
    label, emoji = INTENTS.get(intent, (intent, "📌"))
    return f"{emoji} {label}"


def _csv(value: str) -> list[str]:
    return [v.strip() for v in value.split(",") if v.strip()]


@dataclass
class Config:
    discord_token: str = ""
    ai_provider: str = ""             # gemini | claude | "" (간이 분류)
    gemini_api_key: str = ""
    gemini_model: str = "gemini-flash-latest"
    anthropic_api_key: str = ""
    model: str = "claude-opus-5-5"
    data_dir: Path = Path("./data")
    archive_dir: Path = Path("./data/archive")
    archive_base_url: str = ""
    topic_tags: list[str] = field(default_factory=lambda: _csv(DEFAULT_TAGS))
    confidence_threshold: float = 0.75
    default_rules: bool = True
    show_timings: bool = True         # 검색 결과·공유·자료 정보에 걸린 시간을 표시 (기록은 항상 함)        # 처음 쓸 때 태그·공유 의도 기준 규칙을 미리 등록
    context_messages: int = 3
    prompt_ttl: int = 600
    max_file_mb: int = 25
    extensions: set[str] = field(default_factory=lambda: set(_csv(DEFAULT_EXTS)))
    ocr_max_pages: int = 20
    google_credentials: str = ""      # 서비스 계정 키 또는 OAuth 토큰 (JSON 문자열이나 파일 경로)
    google_folder_id: str = ""        # 자료를 올릴 드라이브 폴더 ID (서비스 계정이면 필수)

    @property
    def db_path(self) -> Path:
        return self.data_dir / "archive.sqlite3"

    @classmethod
    def from_env(cls) -> "Config":
        env = os.environ.get
        data_dir = Path(env("DATA_DIR") or "./data")
        gemini_key = env("GEMINI_API_KEY") or env("GOOGLE_API_KEY") or ""
        anthropic_key = env("ANTHROPIC_API_KEY", "")
        provider = (env("AI_PROVIDER") or "").strip().lower()
        if provider not in ("gemini", "claude"):
            provider = "gemini" if gemini_key else "claude" if anthropic_key else ""
        return cls(
            ai_provider=provider,
            gemini_api_key=gemini_key,
            gemini_model=env("GEMINI_MODEL") or "gemini-flash-latest",
            discord_token=env("DISCORD_TOKEN", ""),
            anthropic_api_key=anthropic_key,
            model=env("ANTHROPIC_MODEL") or "claude-opus-5-5",
            data_dir=data_dir,
            archive_dir=Path(env("ARCHIVE_DIR") or data_dir / "archive"),
            archive_base_url=(env("ARCHIVE_BASE_URL") or "").rstrip("/"),
            topic_tags=_csv(env("TOPIC_TAGS") or DEFAULT_TAGS),
            confidence_threshold=float(env("CONFIDENCE_THRESHOLD") or 0.75),
            default_rules=(env("DEFAULT_RULES") or "1").strip().lower() not in ("0", "false", "no", "off"),
            show_timings=(env("SHOW_TIMINGS") or "1").strip().lower() not in ("0", "false", "no", "off"),
            context_messages=int(env("CONTEXT_MESSAGES") or 3),
            prompt_ttl=int(env("PROMPT_TTL") or 600),
            max_file_mb=int(env("MAX_FILE_MB") or 25),
            extensions={e.lower().lstrip(".") for e in _csv(env("ARCHIVE_EXTENSIONS") or DEFAULT_EXTS)},
            ocr_max_pages=int(env("OCR_MAX_PAGES") or 20),
            google_credentials=(env("GOOGLE_CREDENTIALS") or "").strip(),
            google_folder_id=(env("GOOGLE_DRIVE_FOLDER_ID") or "").strip(),
        )
