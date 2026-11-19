"""디스코드와 무관한 핵심 로직: 보관 → 분류 → 버전 연결 → 검색 → 수정 기록."""
from __future__ import annotations

import asyncio
import csv
import hashlib
import io
import logging
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from . import search as lexical
from .config import INTENTS, Config
from .db import Database
from .extract import (IMAGE_EXTS, LinkContent, extension, extract_file_text, fetch_link, pdf_needs_ocr,
                      prepare_image, trim_pdf)
from .llm import Analysis, AnalyzeInput, HeuristicAnalyzer

log = logging.getLogger(__name__)

OCR_MAX_BYTES = 20 * 1024 * 1024   # AI가 따로 정하지 않았을 때 보낼 수 있는 첨부 최대 크기
VERSION_CANDIDATE_MIN = 0.45   # 이 이상 비슷해야 버전 후보로 AI에게 보여 준다
EXCERPT_CHARS = 4000


@dataclass
class Source:
    guild_id: int
    author_name: str
    channel_id: int | None = None
    message_id: int | None = None
    author_id: int | None = None
    jump_url: str | None = None
    platform: str = "discord"
    created_at: str | None = None


@dataclass
class IngestResult:
    doc: dict
    duplicate: bool = False
    previous: dict | None = None   # 연결된 이전 버전


@dataclass
class SearchHit:
    doc: dict
    reason: str = ""
    latest: dict | None = None     # 이 자료보다 새로운 버전이 있으면 그 자료


def _safe_name(name: str) -> str:
    name = re.sub(r"[\\/:*?\"<>|\x00-\x1f]", "_", name).strip(" .")
    return name[:100] or "file"


class ArchiveService:
    def __init__(self, config: Config, db: Database, analyzer=None, link_fetcher=fetch_link):
        self.config = config
        self.db = db
        self.analyzer = analyzer or HeuristicAnalyzer()
        self.fallback = HeuristicAnalyzer()
        self.fetch_link = link_fetcher
        self._locks: dict[int, asyncio.Lock] = {}
        self.sync = None   # GoogleSync: 설정되어 있으면 변경이 생길 때마다 드라이브·시트에 반영

    # ── 보관 ─────────────────────────────────────────────
    async def ingest_file(self, src: Source, filename: str, data: bytes, context: list[str]) -> IngestResult | None:
        """파일을 보관한다. 보관하지 않은 경우(읽을 수 없는 이미지, 밈·짤) None."""
        ext = extension(filename)
        is_image = ext in IMAGE_EXTS
        vision = getattr(self.analyzer, "vision", False)
        if is_image and not vision:
            return None   # 이미지는 AI가 읽어 보고 자료인지 판단할 수 있을 때만 보관한다
        sha = hashlib.sha256(data).hexdigest()
        async with self._lock(src.guild_id):
            if dup := self._duplicate(src, sha):
                return dup
            text = await asyncio.to_thread(extract_file_text, filename, data)
            attachment = None
            if is_image:
                attachment = await asyncio.to_thread(prepare_image, data)
                if attachment is None:
                    return None
            elif vision and ext == "pdf" and await asyncio.to_thread(pdf_needs_ocr, data, text):
                trimmed = await asyncio.to_thread(trim_pdf, data, self.config.ocr_max_pages)
                if len(trimmed) <= getattr(self.analyzer, "max_attachment_bytes", OCR_MAX_BYTES):
                    attachment = ("application/pdf", trimmed)
            return await self._store(src, "file", filename, None, sha, text, data, context,
                                     attachment=attachment, is_image=is_image)

    async def ingest_link(self, src: Source, url: str, context: list[str]) -> IngestResult:
        sha = hashlib.sha256(url.encode()).hexdigest()
        async with self._lock(src.guild_id):
            if dup := self._duplicate(src, sha):
                return dup
            content: LinkContent = await self.fetch_link(url)
            name = content.title or url
            body = f"# {name}\n\n원문: {url}\n\n{content.text}".encode()
            return await self._store(src, "link", name, url, sha, content.text, body, context)

    def _lock(self, guild_id: int) -> asyncio.Lock:
        # 같은 서버의 자료는 차례로 처리한다 (동시에 올린 v1·v2가 서로를 못 보는 일을 막는다)
        return self._locks.setdefault(guild_id, asyncio.Lock())

    def _duplicate(self, src: Source, sha: str) -> IngestResult | None:
        existing = self.db.find_by_sha(src.guild_id, sha)
        if existing is None:
            return None
        # 같은 자료를 다시 올림 = 못 찾아서 재공유했다는 신호. 지표로만 센다.
        self.db.add_event(src.guild_id, "reshare", src.author_id, existing["id"])
        return IngestResult(doc=existing, duplicate=True)

    async def _store(self, src: Source, kind: str, name: str, url: str | None, sha: str,
                     text: str, data: bytes, context: list[str],
                     attachment: tuple[str, bytes] | None = None, is_image: bool = False) -> IngestResult | None:
        guild = src.guild_id
        # 이미지 이름은 대개 image.png 같은 기본값이라 이름으로 버전을 찾으면 엉뚱하게 묶인다
        candidates = [] if is_image else self._version_candidates(guild, name, text)
        item = AnalyzeInput(
            kind=kind, name=name, text=text, context=context, candidates=candidates,
            glossary=self.db.glossary(guild), rules=[t for _, t in self.db.rules(guild)],
            topic_tags=self.config.topic_tags, attachment=attachment,
        )
        try:
            analysis: Analysis = await self.analyzer.analyze(item)
        except Exception:
            log.exception("AI 분류 실패 — 간이 분류로 대체합니다: %s", name)
            if is_image:
                return None   # 읽어 보지 못한 이미지는 자료인지 알 수 없어 보관하지 않는다
            analysis = await self.fallback.analyze(item)
        if is_image and not analysis.is_material:
            return None
        if attachment and analysis.text_excerpt.strip():
            text = analysis.text_excerpt.strip()   # AI가 읽은 글자를 검색용 본문으로 쓴다

        intent = analysis.intent if analysis.intent in INTENTS else "reference"
        confidence = min(max(float(analysis.confidence), 0.0), 1.0)
        by_id = {c["id"]: c for c in candidates}
        previous = by_id.get(analysis.previous_version_id)   # 후보 밖의 id는 무시

        created = src.created_at or datetime.now(timezone.utc).isoformat(timespec="seconds")
        rel_path = self._write_copy(guild, created, sha, name if kind == "file" else f"{name}.md", data)
        doc_id = self.db.insert_doc(
            guild_id=guild, channel_id=src.channel_id, message_id=src.message_id,
            author_id=src.author_id, author_name=src.author_name, source=src.platform, kind=kind,
            filename=name if kind == "file" else None, url=url, jump_url=src.jump_url,
            archive_path=rel_path, sha256=sha, size=len(data),
            title=analysis.title.strip() or name, summary=analysis.summary.strip(),
            tags=[t.strip() for t in analysis.tags if t.strip()][:5],
            intent=intent, ai_intent=intent, confidence=confidence,
            version_label=analysis.version_label.strip() or None,
            pending_intent=int(confidence < self.config.confidence_threshold),
            excerpt=text[:EXCERPT_CHARS], created_at=created,
        )
        if previous:
            group = previous["version_group_id"] or previous["id"]
            next_no = max(d["version_no"] for d in self.db.version_chain(group)) + 1
            self.db.update_doc(doc_id, version_group_id=group, version_no=next_no,
                               change_note=analysis.change_note.strip() or None, pending_version=1)
        else:
            self.db.update_doc(doc_id, version_group_id=doc_id)
        self.refresh_index(guild)
        return IngestResult(doc=self.db.get_doc(doc_id), previous=previous)

    def _version_candidates(self, guild_id: int, name: str, text: str) -> list[dict]:
        scored = []
        for d in self.db.list_docs(guild_id):
            sim = max(
                lexical.similarity(name, text, d.get("filename") or "", d.get("excerpt") or ""),
                lexical.similarity(name, "", d.get("title") or "", ""),
            )
            if sim >= VERSION_CANDIDATE_MIN:
                scored.append((sim, d))
        scored.sort(key=lambda x: (x[0], x[1]["version_no"]), reverse=True)
        # 같은 계보에서는 가장 최신 버전만 후보로 둔다
        seen: set[int] = set()
        out = []
        for _, d in scored:
            group = d["version_group_id"] or d["id"]
            if group in seen:
                continue
            seen.add(group)
            out.append(max(self.db.version_chain(group) or [d], key=lambda x: x["version_no"]))
        return out[:5]

    def _write_copy(self, guild_id: int, created: str, sha: str, name: str, data: bytes) -> str:
        rel = Path(str(guild_id)) / created[:7] / f"{sha[:8]}_{_safe_name(name)}"
        path = self.config.archive_dir / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return rel.as_posix()

    # ── 사람의 수정 (모두 기록) ───────────────────────────
    def set_intent(self, doc_id: int, intent: str, actor_id: int | None, actor_name: str) -> dict:
        doc = self._require(doc_id)
        if intent not in INTENTS:
            raise ValueError(f"알 수 없는 공유 의도: {intent}")
        self.db.update_doc(doc_id, intent=intent, intent_source="user", pending_intent=0)
        self.db.audit(doc["guild_id"], doc_id, actor_id, actor_name, "intent", doc["intent"], intent)
        self.refresh_index(doc["guild_id"])
        return self.db.get_doc(doc_id)

    def resolve_version(self, doc_id: int, keep: bool, actor_id: int | None, actor_name: str) -> dict:
        doc = self._require(doc_id)
        if keep:
            self.db.update_doc(doc_id, pending_version=0)
            self.db.audit(doc["guild_id"], doc_id, actor_id, actor_name, "version_confirm",
                          None, f"group={doc['version_group_id']} v{doc['version_no']}")
        else:
            self.db.update_doc(doc_id, version_group_id=doc_id, version_no=1, change_note=None, pending_version=0)
            self.db.audit(doc["guild_id"], doc_id, actor_id, actor_name, "version_unlink",
                          f"group={doc['version_group_id']} v{doc['version_no']}", "별개 자료")
        self.refresh_index(doc["guild_id"])
        return self.db.get_doc(doc_id)

    def edit_doc(self, doc_id: int, actor_id: int | None, actor_name: str,
                 title: str | None = None, tags: list[str] | None = None) -> dict:
        doc = self._require(doc_id)
        if title:
            self.db.update_doc(doc_id, title=title)
            self.db.audit(doc["guild_id"], doc_id, actor_id, actor_name, "title", doc["title"], title)
        if tags is not None:
            self.db.update_doc(doc_id, tags=tags)
            self.db.audit(doc["guild_id"], doc_id, actor_id, actor_name, "tags",
                          ", ".join(doc["tags"]), ", ".join(tags))
        self.refresh_index(doc["guild_id"])
        return self.db.get_doc(doc_id)

    def _require(self, doc_id: int) -> dict:
        doc = self.db.get_doc(doc_id)
        if doc is None:
            raise LookupError(f"자료 #{doc_id}를 찾을 수 없어요.")
        return doc

    # ── 검색·공유 ─────────────────────────────────────────
    async def search(self, guild_id: int, user_id: int | None, query: str) -> tuple[int, list[SearchHit]]:
        event_id = self.db.add_event(guild_id, "search", user_id)
        docs = self.db.list_docs(guild_id)
        if not docs:
            return event_id, []
        glossary = self.db.glossary(guild_id)
        ranked = [d for _, d in lexical.rank(query, docs, glossary)]
        if not getattr(self.analyzer, "semantic", False):
            pool = ranked   # 간이 모드: 글자가 겹치는 자료만
        elif len(docs) <= 60:
            # 자료가 적을 때는 전체 목록을 AI에게 보여 줘 표현이 전혀 달라도 찾게 한다
            ranked_ids = {d["id"] for d in ranked}
            pool = ranked + [d for d in docs if d["id"] not in ranked_ids]
        else:
            top_ids = {d["id"] for d in ranked[:30]}
            pool = ranked[:30] + [d for d in docs[:20] if d["id"] not in top_ids]
        try:
            picks = await self.analyzer.pick(query, pool, glossary)
        except Exception:
            log.exception("AI 검색 실패 — 어휘 검색 결과로 대체합니다")
            picks = [(d["id"], "") for d in ranked[:5]]
        by_id = {d["id"]: d for d in docs}
        hits, seen = [], set()
        for doc_id, reason in picks:
            doc = by_id.get(doc_id)
            if doc is None or doc_id in seen:
                continue
            seen.add(doc_id)
            hits.append(SearchHit(doc=doc, reason=reason, latest=self.newer_version(doc)))
        return event_id, hits[:5]

    def newer_version(self, doc: dict) -> dict | None:
        chain = self.db.version_chain(doc["version_group_id"] or doc["id"])
        latest = chain[-1] if chain else doc
        return latest if latest["id"] != doc["id"] else None

    def record_share(self, doc_id: int, user_id: int | None, search_event_id: int | None) -> dict:
        doc = self._require(doc_id)
        elapsed = None
        if search_event_id and (event := self.db.get_event(search_event_id)):
            started = datetime.fromisoformat(event["created_at"])
            elapsed = (datetime.now(timezone.utc) - started).total_seconds()
        self.db.add_event(doc["guild_id"], "share", user_id, doc_id, elapsed)
        self.db.bump_reuse(doc_id)
        return doc

    def archive_file(self, doc: dict) -> Path | None:
        if not doc.get("archive_path"):
            return None
        path = self.config.archive_dir / doc["archive_path"]
        return path if path.is_file() else None

    # ── AI 연결용 색인·내보내기 ───────────────────────────
    EXPORT_HEADER = ["id", "날짜", "제목", "공유 의도", "의도 출처", "태그", "버전", "버전 표기", "바뀐 점",
                     "요약", "올린이", "출처", "종류", "파일명", "링크", "원문 메시지", "드라이브", "사본 경로",
                     "재사용 횟수"]

    def export_rows(self, guild_id: int) -> list[list]:
        rows: list[list] = [list(self.EXPORT_HEADER)]
        for d in reversed(self.db.list_docs(guild_id)):
            rows.append([
                d["id"], d["created_at"][:10], d["title"], INTENTS.get(d["intent"], (d["intent"],))[0],
                "사람" if d["intent_source"] == "user" else "AI", ", ".join(d["tags"]),
                f"v{d['version_no']}", d["version_label"] or "", d["change_note"] or "", d["summary"],
                d["author_name"], d["source"], d["kind"], d["filename"] or "", d["url"] or "",
                d["jump_url"] or "", d.get("drive_url") or "", d["archive_path"] or "", d["reuse_count"],
            ])
        return rows

    def export_csv(self, guild_id: int) -> str:
        buf = io.StringIO()
        csv.writer(buf).writerows(self.export_rows(guild_id))
        return buf.getvalue()

    def refresh_index(self, guild_id: int, schedule_sync: bool = True) -> None:
        """아카이브 폴더에 INDEX.md·index.csv를 다시 쓴다. 이 폴더를 ChatGPT·Gemini·Claude에 연결하면
        AI가 색인을 읽고 필요한 자료를 바로 찾을 수 있다."""
        try:
            root = self.config.archive_dir / str(guild_id)
            root.mkdir(parents=True, exist_ok=True)
            docs = self.db.list_docs(guild_id)
            lines = [
                "# 팀 자료 아카이브 색인", "",
                "팀 디스코드에 올라온 자료를 봇이 자동으로 정리한 목록입니다. "
                "공유 의도는 '왜 올렸는지'를 뜻합니다. 같은 자료의 여러 버전이 있으면 버전 번호가 큰 것이 최신입니다.", "",
            ]
            for d in docs:
                intent = INTENTS.get(d["intent"], (d["intent"],))[0]
                label = f" {d['version_label']}" if d["version_label"] else ""
                lines += [
                    f"## {d['title']} (#{d['id']})",
                    f"- 날짜: {d['created_at'][:10]} · 올린이: {d['author_name']} · 공유 의도: {intent} "
                    f"· 버전: v{d['version_no']}{label} · 태그: {', '.join(d['tags'])}",
                    f"- 요약: {d['summary']}",
                ]
                if d["change_note"]:
                    lines.append(f"- 이전 버전에서 바뀐 점: {d['change_note']}")
                if d["archive_path"]:
                    lines.append(f"- 파일: {d['archive_path'].split('/', 1)[-1]}")
                if d["url"]:
                    lines.append(f"- 링크: {d['url']}")
                if d.get("drive_url"):
                    lines.append(f"- 드라이브: {d['drive_url']}")
                lines.append("")
            terms = self.db.glossary(guild_id)
            if terms:
                lines += ["# 팀 용어 사전", ""] + [f"- {t} = {m}" for t, m in terms] + [""]
            (root / "INDEX.md").write_text("\n".join(lines), encoding="utf-8")
            (root / "index.csv").write_text(self.export_csv(guild_id), encoding="utf-8-sig")
        except OSError:
            log.exception("색인 파일을 쓰지 못했습니다")
        if schedule_sync and self.sync is not None:
            self.sync.schedule(guild_id)
