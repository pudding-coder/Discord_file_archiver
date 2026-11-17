from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS documents (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id INTEGER NOT NULL,
    channel_id INTEGER,
    message_id INTEGER,
    author_id INTEGER,
    author_name TEXT,
    source TEXT NOT NULL DEFAULT 'discord',      -- discord | kakao
    kind TEXT NOT NULL,                          -- file | link
    filename TEXT,
    url TEXT,                                    -- 링크 원문 주소
    jump_url TEXT,                               -- 원문 디스코드 메시지 링크
    archive_path TEXT,                           -- ARCHIVE_DIR 기준 상대 경로 (사본)
    sha256 TEXT NOT NULL,
    size INTEGER,
    title TEXT,
    summary TEXT,
    tags TEXT NOT NULL DEFAULT '[]',
    intent TEXT,
    ai_intent TEXT,                              -- AI가 처음 붙인 값 (수정률 계산용)
    confidence REAL,
    intent_source TEXT NOT NULL DEFAULT 'ai',    -- ai | user
    version_label TEXT,
    version_group_id INTEGER,
    version_no INTEGER NOT NULL DEFAULT 1,
    change_note TEXT,
    pending_intent INTEGER NOT NULL DEFAULT 0,
    pending_version INTEGER NOT NULL DEFAULT 0,
    excerpt TEXT,
    reuse_count INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_doc_sha ON documents(guild_id, sha256);
CREATE INDEX IF NOT EXISTS idx_doc_msg ON documents(message_id);
CREATE INDEX IF NOT EXISTS idx_doc_group ON documents(version_group_id);

CREATE TABLE IF NOT EXISTS glossary (
    guild_id INTEGER NOT NULL, term TEXT NOT NULL, meaning TEXT NOT NULL,
    created_by TEXT, created_at TEXT NOT NULL,
    PRIMARY KEY (guild_id, term)
);
CREATE TABLE IF NOT EXISTS rules (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id INTEGER NOT NULL, text TEXT NOT NULL,
    created_by TEXT, created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS channel_settings (
    guild_id INTEGER NOT NULL, channel_id INTEGER NOT NULL, collect INTEGER NOT NULL,
    PRIMARY KEY (guild_id, channel_id)
);
-- 모든 수정 기록 (책임 소재): 누가 무엇을 무엇에서 무엇으로 바꿨는지
CREATE TABLE IF NOT EXISTS audit_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id INTEGER NOT NULL, doc_id INTEGER, actor_id INTEGER, actor_name TEXT,
    action TEXT NOT NULL, before TEXT, after TEXT, created_at TEXT NOT NULL
);
-- 지표용 이벤트: search | share | rerequest | reshare (메시지 본문은 저장하지 않음)
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id INTEGER NOT NULL, type TEXT NOT NULL, user_id INTEGER,
    doc_id INTEGER, value REAL, created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_events ON events(guild_id, type, created_at);
"""


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _doc(row: sqlite3.Row | None) -> dict | None:
    if row is None:
        return None
    d = dict(row)
    d["tags"] = json.loads(d.get("tags") or "[]")
    return d


class Database:
    def __init__(self, path: Path | str):
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(path))
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()

    # ── 자료 ─────────────────────────────────────────────
    def insert_doc(self, **fields) -> int:
        fields = dict(fields)
        fields["tags"] = json.dumps(fields.get("tags") or [], ensure_ascii=False)
        fields.setdefault("created_at", now_iso())
        cols = ", ".join(fields)
        marks = ", ".join("?" for _ in fields)
        cur = self.conn.execute(f"INSERT INTO documents ({cols}) VALUES ({marks})", list(fields.values()))
        self.conn.commit()
        return int(cur.lastrowid)

    def update_doc(self, doc_id: int, **fields) -> None:
        if "tags" in fields:
            fields["tags"] = json.dumps(fields["tags"] or [], ensure_ascii=False)
        sets = ", ".join(f"{k} = ?" for k in fields)
        self.conn.execute(f"UPDATE documents SET {sets} WHERE id = ?", [*fields.values(), doc_id])
        self.conn.commit()

    def get_doc(self, doc_id: int) -> dict | None:
        return _doc(self.conn.execute("SELECT * FROM documents WHERE id = ?", (doc_id,)).fetchone())

    def find_by_sha(self, guild_id: int, sha: str) -> dict | None:
        return _doc(
            self.conn.execute("SELECT * FROM documents WHERE guild_id = ? AND sha256 = ?", (guild_id, sha)).fetchone()
        )

    def docs_by_message(self, message_id: int) -> list[dict]:
        rows = self.conn.execute("SELECT * FROM documents WHERE message_id = ? ORDER BY id", (message_id,))
        return [_doc(r) for r in rows]

    def list_docs(self, guild_id: int, limit: int | None = None) -> list[dict]:
        sql = "SELECT * FROM documents WHERE guild_id = ? ORDER BY created_at DESC, id DESC"
        if limit:
            sql += f" LIMIT {int(limit)}"
        return [_doc(r) for r in self.conn.execute(sql, (guild_id,))]

    def version_chain(self, group_id: int) -> list[dict]:
        rows = self.conn.execute(
            "SELECT * FROM documents WHERE version_group_id = ? ORDER BY version_no, id", (group_id,)
        )
        return [_doc(r) for r in rows]

    def bump_reuse(self, doc_id: int) -> None:
        self.conn.execute("UPDATE documents SET reuse_count = reuse_count + 1 WHERE id = ?", (doc_id,))
        self.conn.commit()

    # ── 팀 용어·규칙 ──────────────────────────────────────
    def set_term(self, guild_id: int, term: str, meaning: str, by: str) -> None:
        self.conn.execute(
            "INSERT INTO glossary VALUES (?,?,?,?,?) ON CONFLICT(guild_id, term) "
            "DO UPDATE SET meaning = excluded.meaning, created_by = excluded.created_by",
            (guild_id, term, meaning, by, now_iso()),
        )
        self.conn.commit()

    def delete_term(self, guild_id: int, term: str) -> bool:
        cur = self.conn.execute("DELETE FROM glossary WHERE guild_id = ? AND term = ?", (guild_id, term))
        self.conn.commit()
        return cur.rowcount > 0

    def glossary(self, guild_id: int) -> list[tuple[str, str]]:
        rows = self.conn.execute("SELECT term, meaning FROM glossary WHERE guild_id = ? ORDER BY term", (guild_id,))
        return [(r["term"], r["meaning"]) for r in rows]

    def add_rule(self, guild_id: int, text: str, by: str) -> int:
        cur = self.conn.execute(
            "INSERT INTO rules (guild_id, text, created_by, created_at) VALUES (?,?,?,?)",
            (guild_id, text, by, now_iso()),
        )
        self.conn.commit()
        return int(cur.lastrowid)

    def delete_rule(self, guild_id: int, rule_id: int) -> bool:
        cur = self.conn.execute("DELETE FROM rules WHERE guild_id = ? AND id = ?", (guild_id, rule_id))
        self.conn.commit()
        return cur.rowcount > 0

    def rules(self, guild_id: int) -> list[tuple[int, str]]:
        rows = self.conn.execute("SELECT id, text FROM rules WHERE guild_id = ? ORDER BY id", (guild_id,))
        return [(r["id"], r["text"]) for r in rows]

    # ── 채널별 수집 설정 ──────────────────────────────────
    def set_collect(self, guild_id: int, channel_id: int, on: bool) -> None:
        self.conn.execute(
            "INSERT INTO channel_settings VALUES (?,?,?) ON CONFLICT(guild_id, channel_id) "
            "DO UPDATE SET collect = excluded.collect",
            (guild_id, channel_id, int(on)),
        )
        self.conn.commit()

    def collect_enabled(self, guild_id: int, channel_id: int) -> bool:
        row = self.conn.execute(
            "SELECT collect FROM channel_settings WHERE guild_id = ? AND channel_id = ?", (guild_id, channel_id)
        ).fetchone()
        return True if row is None else bool(row["collect"])

    # ── 기록·지표 ─────────────────────────────────────────
    def audit(self, guild_id: int, doc_id: int | None, actor_id: int | None, actor_name: str,
              action: str, before: str | None, after: str | None) -> None:
        self.conn.execute(
            "INSERT INTO audit_log (guild_id, doc_id, actor_id, actor_name, action, before, after, created_at) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (guild_id, doc_id, actor_id, actor_name, action, before, after, now_iso()),
        )
        self.conn.commit()

    def audit_for(self, doc_id: int) -> list[dict]:
        rows = self.conn.execute("SELECT * FROM audit_log WHERE doc_id = ? ORDER BY id", (doc_id,))
        return [dict(r) for r in rows]

    def add_event(self, guild_id: int, type_: str, user_id: int | None = None,
                  doc_id: int | None = None, value: float | None = None) -> int:
        cur = self.conn.execute(
            "INSERT INTO events (guild_id, type, user_id, doc_id, value, created_at) VALUES (?,?,?,?,?,?)",
            (guild_id, type_, user_id, doc_id, value, now_iso()),
        )
        self.conn.commit()
        return int(cur.lastrowid)

    def get_event(self, event_id: int) -> dict | None:
        row = self.conn.execute("SELECT * FROM events WHERE id = ?", (event_id,)).fetchone()
        return dict(row) if row else None

    def stats(self, guild_id: int, days: int) -> dict:
        since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat(timespec="seconds")
        q = lambda sql, *a: self.conn.execute(sql, (guild_id, since, *a)).fetchone()[0]  # noqa: E731
        docs = q("SELECT COUNT(*) FROM documents WHERE guild_id = ? AND created_at >= ?")
        corrected = q(
            "SELECT COUNT(*) FROM documents WHERE guild_id = ? AND created_at >= ? "
            "AND intent_source = 'user' AND intent != ai_intent"
        )
        reused = q("SELECT COUNT(*) FROM documents WHERE guild_id = ? AND created_at >= ? AND reuse_count > 0")
        count = lambda t: q("SELECT COUNT(*) FROM events WHERE guild_id = ? AND created_at >= ? AND type = ?", t)  # noqa: E731
        times = sorted(
            r[0] for r in self.conn.execute(
                "SELECT value FROM events WHERE guild_id = ? AND created_at >= ? AND type = 'share' "
                "AND value IS NOT NULL", (guild_id, since))
        )
        median = None
        if times:
            mid = len(times) // 2
            median = times[mid] if len(times) % 2 else (times[mid - 1] + times[mid]) / 2
        return {
            "days": days, "docs": docs, "corrected": corrected, "reused": reused,
            "searches": count("search"), "shares": count("share"),
            "rerequests": count("rerequest"), "reshares": count("reshare"),
            "median_search_seconds": median,
        }
