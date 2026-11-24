"""디스코드 연결부: 메시지 수집, 확인 버튼, 슬래시 명령."""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
import logging
import time
import re

import discord
from discord import app_commands

from .config import INTENTS, Config, intent_label
from .extract import extension, find_urls
from .service import ArchiveService, IngestResult, SearchHit, Source

log = logging.getLogger(__name__)

# "다시 올려줘", "어디 있어?" 같은 재요청 메시지. 횟수만 세고 본문은 저장하지 않는다.
REREQUEST_RE = re.compile(r"다시\s*(올려|보내|공유)|어디\s*(있|갔|에\s*있)|어딨|못\s*찾겠|(링크|파일|자료)\s*좀")
MAX_ITEMS_PER_MESSAGE = 5


def original_filename(att) -> str:
    """디스코드는 한글 등 영문이 아닌 파일명을 무작위 문자열(filename)로 바꾸고,
    원래 이름은 title에 따로 준다. 원래 이름을 쓰되 확장자는 filename 쪽을 따른다."""
    name = att.filename
    title = (getattr(att, "title", None) or "").strip()
    if not title:
        return name
    ext = extension(name)
    if ext and extension(title) != ext:
        title = f"{title}.{ext}"
    return title


# ── 화면 문구 ────────────────────────────────────────────
def version_text(doc: dict) -> str:
    label = f" {doc['version_label']}" if doc.get("version_label") else ""
    return f"v{doc['version_no']}{label}" if doc["version_no"] > 1 or label else ""


def one_line(doc: dict) -> str:
    bits = [f"**{doc['title']}**", intent_label(doc["intent"])]
    if v := version_text(doc):
        bits.append(v)
    return " · ".join(bits)


MEASURE_WINDOW = 15 * 60      # /측정 시작 후 이 시간(초) 안에 공유해야 측정으로 친다
PROCESS_CAP = 600             # 이보다 오래 걸렸으면 봇이 꺼져 있던 것: 처리 시간으로 기록하지 않음


def fmt_seconds(sec: float | None) -> str:
    if sec is None:
        return "–"
    if sec < 10:
        return f"{sec:.1f}초"
    if sec < 60:
        return f"{sec:.0f}초"
    return f"{int(sec // 60)}분 {int(sec % 60)}초"


def render_prompt(service: ArchiveService, doc: dict) -> str:
    took = f" (정리 {fmt_seconds(doc['process_seconds'])})" \
        if service.config.show_timings and doc.get("process_seconds") is not None else ""
    lines = [f"📌 {one_line(doc)} 저장했어요.{took}"]
    if doc["pending_intent"]:
        label = INTENTS[doc["intent"]][0]
        lines.append(f"왜 올린 자료인지 헷갈려요. 일단 **{label}**로 뒀는데, 다르면 눌러 주세요.")
    if doc.get("pending_tags"):
        tags = ", ".join(doc["tags"]) or "없음"
        lines.append(f"주제도 헷갈려요. 일단 **{tags}** 태그를 붙였는데, 다르면 메뉴에서 골라 주세요.")
    if doc["pending_intent"] or doc.get("pending_tags"):
        lines.append("(안 고르면 그대로 둬요)")
    if doc["pending_version"]:
        chain = service.db.version_chain(doc["version_group_id"])
        prev = next((d for d in reversed(chain) if d["id"] != doc["id"]), None)
        if prev:
            note = f" 바뀐 점: {doc['change_note']}" if doc.get("change_note") else ""
            lines.append(f"📎 「{prev['title']}」의 **v{doc['version_no']}**로 연결했어요.{note}")
    return "\n".join(lines)


def render_info(service: ArchiveService, doc: dict) -> str:
    lines = [
        f"📌 {one_line(doc)}  `#{doc['id']}`",
        f"> {doc['summary']}",
        f"태그: {', '.join(doc['tags']) or '없음'} · 올린이: {doc['author_name']} · {doc['created_at'][:10]} "
        f"· 의도 지정: {'사람' if doc['intent_source'] == 'user' else 'AI'}"
        + (f" · 정리 {fmt_seconds(doc['process_seconds'])}"
           if service.config.show_timings and doc.get("process_seconds") is not None else ""),
    ]
    chain = service.db.version_chain(doc["version_group_id"] or doc["id"])
    if len(chain) > 1:
        lines.append("버전: " + " → ".join(
            f"{'**' if d['id'] == doc['id'] else ''}v{d['version_no']} {d['title']}{'**' if d['id'] == doc['id'] else ''}"
            for d in chain))
    if doc.get("jump_url"):
        lines.append(f"[원문 메시지]({doc['jump_url']})")
    lines.append("공유 의도·태그·제목이 다르면 아래에서 고쳐 주세요. 잘못 보관된 자료는 🗑️ 삭제로 뺄 수 있어요.")
    return "\n".join(lines)


def render_hit(index: int, hit: SearchHit, base_url: str) -> str:
    doc = hit.doc
    lines = [f"**{index}.** {one_line(doc)} · {doc['created_at'][:10]} · {doc['author_name']}"]
    if hit.latest:
        lines.append(f"⚠️ 최신본이 아니에요. 최신: **v{hit.latest['version_no']} {hit.latest['title']}**")
    lines.append(f"> {hit.reason or doc['summary']}")
    lines.append(" · ".join(doc_links(doc, base_url)) or "링크 없음 (공유하면 사본 파일을 올려요)")
    return "\n".join(lines)


KST = timezone(timedelta(hours=9))


def local_date(iso: str) -> str:
    """저장된 UTC 시각을 한국 날짜로 (예: 11/18)."""
    try:
        when = datetime.fromisoformat(iso)
    except (TypeError, ValueError):
        return (iso or "")[5:10].replace("-", "/")
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    when = when.astimezone(KST)
    return f"{when.month}/{when.day}"


def render_duplicates(service: ArchiveService, docs: list[dict]) -> str:
    lines = []
    for doc in docs[:MAX_ITEMS_PER_MESSAGE]:
        links = []
        if doc.get("jump_url"):
            links.append(f"[원문 메시지]({doc['jump_url']})")
        if doc.get("drive_url"):
            links.append(f"[드라이브]({doc['drive_url']})")
        line = (f"♻️ 이미 {local_date(doc['created_at'])}에 {doc['author_name']}님이 올린 자료예요: "
                f"**{doc['title']}**")
        if links:
            line += " → " + " · ".join(links)
        if latest := service.newer_version(doc):
            latest_link = f" → [원문 메시지]({latest['jump_url']})" if latest.get("jump_url") else ""
            line += f"\n　최신본은 **v{latest['version_no']} {latest['title']}**예요{latest_link}"
        lines.append(line)
    return "\n".join(lines)[:2000]


def doc_links(doc: dict, base_url: str) -> list[str]:
    links = []
    if doc.get("jump_url"):
        links.append(f"[원문 메시지]({doc['jump_url']})")
    if doc.get("url"):
        links.append(f"[링크 열기]({doc['url']})")
    if doc.get("drive_url"):
        links.append(f"[드라이브에서 열기]({doc['drive_url']})")
    elif base_url and doc.get("archive_path"):
        links.append(f"[드라이브 폴더]({base_url}) `{doc['archive_path'].split('/', 1)[-1]}`")
    return links


def render_timing(timing: dict) -> str:
    """/통계의 구간별 시간 (중앙값, 괄호는 측정 건수)."""
    def row(label: str, key: str) -> str:
        value, n = timing[key]
        return f"- {label}: {fmt_seconds(value)} ({n}건)"

    return "\n".join([
        "**걸린 시간 (중앙값)**",
        row("T1 정리 (올림 → 📌)", "process"),
        row("T2 봇 응답 (`/찾아줘` → 결과)", "response"),
        row("T3 고르기 (결과 → 공유)", "pick"),
        row("T0 검색어 입력 (`/측정`)", "typing"),
        row("전체 찾는 시간 (`/측정`)", "measure"),
    ])


def render_tags(topic_tags: list[str], counts: list[tuple[str, int]]) -> str:
    lines = ["**기본 주제 태그** (AI가 여기서 골라요)", ", ".join(topic_tags) or "없음", ""]
    if counts:
        lines.append("**자료에 붙은 태그** (자료 수)")
        lines.append(", ".join(f"{tag} {n}" for tag, n in counts[:40]))
        extra = [tag for tag, _ in counts if tag not in topic_tags]
        if extra:
            lines.append(f"기본 목록에 없는 태그: {', '.join(extra[:20])} (팀 규칙이나 자료 정보에서 붙인 태그)")
    else:
        lines.append("아직 태그가 붙은 자료가 없어요.")
    lines += ["", "새 태그를 쓰게 하려면 `/규칙 등록`으로 알려 주거나, 봇 설정의 `TOPIC_TAGS`에 추가하세요."]
    return "\n".join(lines)[:2000]


# ── 버튼 (custom_id에 상태를 담아 봇을 재시작해도 동작) ──────
class IntentButton(discord.ui.DynamicItem[discord.ui.Button], template=r"intent:(?P<doc>\d+):(?P<intent>\w+):(?P<mode>[pi])"):
    def __init__(self, doc_id: int, intent: str, mode: str, selected: bool = False):
        label, emoji = INTENTS[intent]
        super().__init__(discord.ui.Button(
            label=label, emoji=emoji, row=0,
            style=discord.ButtonStyle.primary if selected else discord.ButtonStyle.secondary,
            custom_id=f"intent:{doc_id}:{intent}:{mode}",
        ))
        self.doc_id, self.intent, self.mode = doc_id, intent, mode

    @classmethod
    async def from_custom_id(cls, interaction, item, match):
        return cls(int(match["doc"]), match["intent"], match["mode"])

    async def callback(self, interaction: discord.Interaction):
        service: ArchiveService = interaction.client.service  # type: ignore[attr-defined]
        try:
            doc = service.set_intent(self.doc_id, self.intent, interaction.user.id, interaction.user.display_name)
        except (LookupError, ValueError) as e:
            return await interaction.response.send_message(str(e), ephemeral=True)
        await _refresh_panel(interaction, service, doc, self.mode)


class VersionButton(discord.ui.DynamicItem[discord.ui.Button], template=r"ver:(?P<doc>\d+):(?P<action>keep|split)"):
    def __init__(self, doc_id: int, action: str):
        keep = action == "keep"
        super().__init__(discord.ui.Button(
            label="맞아요, 새 버전" if keep else "별개 자료예요", row=2,
            style=discord.ButtonStyle.primary if keep else discord.ButtonStyle.secondary,
            custom_id=f"ver:{doc_id}:{action}",
        ))
        self.doc_id, self.action = doc_id, action

    @classmethod
    async def from_custom_id(cls, interaction, item, match):
        return cls(int(match["doc"]), match["action"])

    async def callback(self, interaction: discord.Interaction):
        service: ArchiveService = interaction.client.service  # type: ignore[attr-defined]
        try:
            doc = service.resolve_version(self.doc_id, self.action == "keep",
                                          interaction.user.id, interaction.user.display_name)
        except LookupError as e:
            return await interaction.response.send_message(str(e), ephemeral=True)
        await _refresh_panel(interaction, service, doc, "p")


class ShareButton(discord.ui.DynamicItem[discord.ui.Button], template=r"share:(?P<doc>\d+):(?P<event>\d+)"):
    def __init__(self, doc_id: int, event_id: int, index: int = 0, channel: str = ""):
        where = f"#{channel}"[:40] if channel else "채널"
        super().__init__(discord.ui.Button(
            label=f"{index}번 자료 {where}에 공유" if index else f"{where}에 공유", emoji="📣",
            style=discord.ButtonStyle.primary if index == 1 else discord.ButtonStyle.secondary,
            custom_id=f"share:{doc_id}:{event_id}",
        ))
        self.doc_id, self.event_id = doc_id, event_id

    @classmethod
    async def from_custom_id(cls, interaction, item, match):
        return cls(int(match["doc"]), int(match["event"]))

    async def callback(self, interaction: discord.Interaction):
        client: ArchiveBot = interaction.client  # type: ignore[assignment]
        service = client.service
        try:
            shared = service.record_share(self.doc_id, interaction.user.id, self.event_id)
        except LookupError as e:
            return await interaction.response.send_message(str(e), ephemeral=True)
        doc = shared.doc
        measured = client.finish_measure(interaction, shared)
        # 파일을 올리면 3초를 넘길 수 있으니 먼저 "처리 중"으로 응답해 둔다
        await interaction.response.defer(ephemeral=True, thinking=True)
        lines = [f"🔎 {interaction.user.mention}님이 찾은 자료: {one_line(doc)}", f"> {doc['summary']}"]
        links = doc_links(doc, client.config.archive_base_url)
        files = []
        if doc["kind"] == "file" and (path := service.archive_file(doc)):
            limit = getattr(interaction.guild, "filesize_limit", 10 * 1024 * 1024)
            if path.stat().st_size <= limit:
                files.append(discord.File(path, filename=doc["filename"] or path.name))
            else:
                lines.append("(파일이 커서 채널에 직접 올리지 못했어요. 아래 링크로 열어 주세요)")
        if links:
            lines.append(" · ".join(links))
        if client.config.show_timings and shared.total is not None:
            lines.append(f"⏱ 찾는 데 {fmt_seconds(shared.total)}")
        try:
            await interaction.channel.send("\n".join(lines), files=files, suppress_embeds=True)  # type: ignore[union-attr]
        except discord.Forbidden:
            return await interaction.followup.send(
                "이 채널에 메시지나 파일을 보낼 권한이 없어요. 봇에 '메시지 보내기'와 '파일 첨부' 권한을 주세요.",
                ephemeral=True)
        except discord.HTTPException:
            log.exception("공유 실패: 자료 #%s", doc["id"])
            return await interaction.followup.send("공유하다 문제가 생겼어요. 잠시 후 다시 눌러 주세요.", ephemeral=True)
        name = getattr(interaction.channel, "name", None)
        done = (f"#{name}에 공유했어요." if name else "채널에 공유했어요.") + (" (파일 포함)" if files else "")
        if measured:
            done += "\n" + measured
        await interaction.followup.send(done, ephemeral=True)


class TagSelect(discord.ui.DynamicItem[discord.ui.Select], template=r"tags:(?P<doc>\d+):(?P<mode>[pi])"):
    """주제 태그 고르기 (최대 3개). 기본 주제 태그 + 이 자료에 이미 붙은 태그를 보여 준다."""

    def __init__(self, doc_id: int, mode: str, options: list[str] | None = None, selected: list[str] | None = None):
        choices = list(dict.fromkeys([*(options or []), *(selected or [])]))[:25] or ["기타"]
        super().__init__(discord.ui.Select(
            custom_id=f"tags:{doc_id}:{mode}", row=1, placeholder="주제 태그 고르기 (최대 3개)",
            min_values=1, max_values=min(3, len(choices)),
            options=[discord.SelectOption(label=t[:100], value=t[:100], default=t in (selected or []))
                     for t in choices],
        ))
        self.doc_id, self.mode = doc_id, mode

    @classmethod
    async def from_custom_id(cls, interaction, item, match):
        return cls(int(match["doc"]), match["mode"])

    async def callback(self, interaction: discord.Interaction):
        service: ArchiveService = interaction.client.service  # type: ignore[attr-defined]
        try:
            doc = service.set_tags(self.doc_id, list(self.item.values),
                                   interaction.user.id, interaction.user.display_name)
        except (LookupError, ValueError) as e:
            return await interaction.response.send_message(str(e), ephemeral=True)
        await _refresh_panel(interaction, service, doc, self.mode)


class TitleModal(discord.ui.Modal):
    def __init__(self, doc: dict):
        super().__init__(title="제목 고치기", timeout=300)
        self.doc_id = doc["id"]
        self.new_title = discord.ui.TextInput(label="새 제목", default=(doc.get("title") or "")[:100],
                                              min_length=1, max_length=100)
        self.add_item(self.new_title)

    async def on_submit(self, interaction: discord.Interaction):
        service: ArchiveService = interaction.client.service  # type: ignore[attr-defined]
        try:
            doc = service.edit_doc(self.doc_id, interaction.user.id, interaction.user.display_name,
                                   title=str(self.new_title.value).strip())
        except LookupError as e:
            return await interaction.response.send_message(str(e), ephemeral=True)
        await interaction.response.edit_message(content=render_info(service, doc),
                                                view=build_panel_view(doc, "i", service.config.topic_tags))


class TitleButton(discord.ui.DynamicItem[discord.ui.Button], template=r"title:(?P<doc>\d+)"):
    def __init__(self, doc_id: int):
        super().__init__(discord.ui.Button(label="제목 고치기", emoji="✏️", row=3,
                                           style=discord.ButtonStyle.secondary, custom_id=f"title:{doc_id}"))
        self.doc_id = doc_id

    @classmethod
    async def from_custom_id(cls, interaction, item, match):
        return cls(int(match["doc"]))

    async def callback(self, interaction: discord.Interaction):
        service: ArchiveService = interaction.client.service  # type: ignore[attr-defined]
        doc = service.db.get_doc(self.doc_id)
        if doc is None:
            return await interaction.response.send_message(f"자료 #{self.doc_id}를 찾을 수 없어요.", ephemeral=True)
        await interaction.response.send_modal(TitleModal(doc))


def can_delete(interaction: discord.Interaction, doc: dict) -> bool:
    """올린 사람이거나 메시지 관리 권한이 있는 사람만 지울 수 있다."""
    if interaction.user.id == doc.get("author_id"):
        return True
    perms = getattr(interaction.user, "guild_permissions", None)
    return bool(perms and (perms.manage_messages or perms.administrator))


class DeleteButton(discord.ui.DynamicItem[discord.ui.Button], template=r"del:(?P<doc>\d+):(?P<step>ask|yes|no)"):
    """'자료 정보'의 🗑 삭제 → 한 번 더 묻고(ask) → 지운다(yes) / 그만둔다(no)."""
    def __init__(self, doc_id: int, step: str = "ask"):
        label, style, emoji = {
            "ask": ("삭제", discord.ButtonStyle.danger, "🗑️"),
            "yes": ("네, 지울게요", discord.ButtonStyle.danger, None),
            "no": ("그만두기", discord.ButtonStyle.secondary, None),
        }[step]
        super().__init__(discord.ui.Button(label=label, style=style, emoji=emoji, row=3,
                                           custom_id=f"del:{doc_id}:{step}"))
        self.doc_id, self.step = doc_id, step

    @classmethod
    async def from_custom_id(cls, interaction, item, match):
        return cls(int(match["doc"]), match["step"])

    async def callback(self, interaction: discord.Interaction):
        service: ArchiveService = interaction.client.service  # type: ignore[attr-defined]
        if self.step == "no":
            return await interaction.response.edit_message(content="지우지 않았어요.", view=None)
        doc = service.db.get_doc(self.doc_id)
        if doc is None:
            return await interaction.response.send_message(f"자료 #{self.doc_id}는 이미 지워졌어요.", ephemeral=True)
        if not can_delete(interaction, doc):
            return await interaction.response.send_message(
                "자료를 올린 사람이나 메시지 관리 권한이 있는 사람만 지울 수 있어요.", ephemeral=True)
        if self.step == "ask":
            view = discord.ui.View(timeout=None)
            view.add_item(DeleteButton(self.doc_id, "yes"))
            view.add_item(DeleteButton(self.doc_id, "no"))
            where = "목록·검색·시트에서 빠지고, 드라이브 파일은 휴지통으로 가요." if doc.get("drive_file_id") \
                else "목록·검색·시트에서 빠져요."
            return await interaction.response.send_message(
                f"🗑️ {one_line(doc)} 을(를) 지울까요?\n{where} 디스코드 원문 메시지는 그대로 남아요.",
                view=view, ephemeral=True, suppress_embeds=True)
        service.delete_doc(self.doc_id, interaction.user.id, interaction.user.display_name)
        await interaction.response.edit_message(
            content=f"🗑️ 「{doc['title']}」을(를) 지웠어요. 같은 파일을 다시 올리면 새로 보관돼요.", view=None)
        await _unmark_message(interaction.client, doc)


async def _unmark_message(client: discord.Client, doc: dict) -> None:
    """원문 메시지에 봇이 붙인 📌·♻️ 표시를 뗀다 (같은 메시지의 다른 자료가 남아 있으면 그대로 둔다)."""
    service: ArchiveService = client.service  # type: ignore[attr-defined]
    if not doc.get("message_id") or service.db.docs_by_message(doc["message_id"]):
        return
    try:
        channel = client.get_channel(doc["channel_id"]) or await client.fetch_channel(doc["channel_id"])
        message = await channel.fetch_message(doc["message_id"])
        for emoji in ("📌", "♻️"):
            await message.remove_reaction(emoji, client.user)
    except (discord.HTTPException, AttributeError):
        pass


def build_panel_view(doc: dict, mode: str, topic_tags: list[str] | tuple = ()) -> discord.ui.View | None:
    """mode p: 자료를 올린 직후의 확인 메시지(애매한 것만 묻는다) / i: '자료 정보' 패널(항상 수정 가능)."""
    view = discord.ui.View(timeout=None)
    if mode == "i" or doc["pending_intent"]:
        for intent in INTENTS:
            view.add_item(IntentButton(doc["id"], intent, mode, selected=intent == doc["intent"]))
    if mode == "i" or doc.get("pending_tags"):
        view.add_item(TagSelect(doc["id"], mode, list(topic_tags), doc["tags"]))
    if doc["pending_version"]:
        view.add_item(VersionButton(doc["id"], "keep"))
        view.add_item(VersionButton(doc["id"], "split"))
    if mode == "i":
        view.add_item(TitleButton(doc["id"]))
        view.add_item(DeleteButton(doc["id"]))
    return view if view.children else None


async def _refresh_panel(interaction: discord.Interaction, service: ArchiveService, doc: dict, mode: str) -> None:
    tags = service.config.topic_tags
    if mode == "i":
        return await interaction.response.edit_message(content=render_info(service, doc),
                                                       view=build_panel_view(doc, "i", tags))
    view = build_panel_view(doc, "p", tags)
    if view:
        return await interaction.response.edit_message(content=render_prompt(service, doc), view=view)
    # 물어볼 것이 끝나면 한 줄로 줄이고 곧 지워 대화를 방해하지 않는다
    await interaction.response.edit_message(
        content=f"📌 {one_line(doc)} · 확인: {interaction.user.display_name}", view=None, delete_after=20)


# ── 봇 ───────────────────────────────────────────────────
class ArchiveBot(discord.Client):
    def __init__(self, config: Config, service: ArchiveService):
        intents = discord.Intents.default()
        intents.message_content = True   # 첨부파일·링크·맥락을 읽으려면 필요 (Developer Portal에서도 켜야 함)
        super().__init__(intents=intents, allowed_mentions=discord.AllowedMentions.none())
        self.config = config
        self.service = service
        self.tree = app_commands.CommandTree(self)
        self.measure_starts: dict[tuple[int, int], datetime] = {}   # (서버, 사용자) → /측정 시작 시각
        register_commands(self)

    async def setup_hook(self) -> None:
        self.add_dynamic_items(IntentButton, VersionButton, ShareButton, TagSelect, TitleButton, DeleteButton)
        if self.service.sync is not None:
            self.service.sync.guild_name = lambda gid: getattr(self.get_guild(gid), "name", None) or str(gid)
            self._drive_task = asyncio.create_task(self.service.sync.run_forever())

    async def on_ready(self) -> None:
        log.info("로그인: %s (서버 %d곳)", self.user, len(self.guilds))
        if getattr(self, "_commands_synced", False):
            return   # 연결이 끊겼다 다시 붙을 때도 on_ready가 오므로 처음 한 번만
        self._commands_synced = True
        # 명령은 서버마다 등록한다: 바로 뜨고, 초대만 하면 새 서버에도 자동으로 생긴다.
        # 예전에 전역으로 등록된 명령이 있으면 두 번씩 보이므로 지운다.
        try:
            await self.http.bulk_upsert_global_commands(self.application_id, payload=[])
        except discord.HTTPException:
            log.warning("전역 명령을 정리하지 못했습니다", exc_info=True)
        for guild in self.guilds:
            await self.register_commands(guild)

    async def on_guild_join(self, guild: discord.Guild) -> None:
        log.info("새 서버에 들어왔습니다: %s", guild.name)
        await self.register_commands(guild)

    async def register_commands(self, guild: discord.abc.Snowflake) -> bool:
        try:
            self.tree.copy_global_to(guild=guild)
            await self.tree.sync(guild=guild)
            log.info("명령 등록: %s", getattr(guild, "name", guild.id))
            return True
        except discord.HTTPException:
            # 권한이 없는 서버 하나 때문에 봇 전체가 멈추지 않게 한다
            log.warning("서버 %s에 명령을 등록하지 못했습니다 (봇을 다시 초대해 보세요)", guild.id, exc_info=True)
            return False

    async def on_message(self, message: discord.Message) -> None:
        if message.author.bot or message.guild is None:
            return
        if not self.service.db.collect_enabled(message.guild.id, message.channel.id):
            return
        try:
            archived = await self.archive_message(message)
        except Exception:
            log.exception("메시지 처리 실패: %s", message.jump_url)
            return
        if not archived and REREQUEST_RE.search(message.content or ""):
            self.service.db.add_event(message.guild.id, "rerequest", message.author.id)

    async def archive_message(self, message: discord.Message, quiet: bool = False) -> int:
        """메시지의 파일·링크를 보관한다. quiet이면 확인 메시지 없이 반응만 남긴다(과거 기록 수집용)."""
        max_bytes = self.config.max_file_mb * 1024 * 1024
        # 다른 채널에서 "전달"한 메시지는 파일·본문이 스냅샷 안에 들어 있다
        snapshots = list(getattr(message, "message_snapshots", None) or [])
        all_attachments = list(message.attachments) + [a for snap in snapshots for a in snap.attachments]
        attachments = [a for a in all_attachments
                       if extension(a.filename) in self.config.extensions and a.size <= max_bytes]
        text = "\n".join([message.content or "", *(snap.content or "" for snap in snapshots)])
        urls = find_urls(text)
        if not attachments and not urls:
            return 0   # 파일·링크가 없는 메시지는 읽지도 저장하지도 않는다

        context = await self._context(message)
        src = Source(
            guild_id=message.guild.id, channel_id=message.channel.id, message_id=message.id,  # type: ignore[union-attr]
            author_id=message.author.id, author_name=message.author.display_name,
            jump_url=message.jump_url, created_at=message.created_at.isoformat(timespec="seconds"),
        )
        try:
            await message.add_reaction("⏳")   # 바로 알아봤다는 표시. 정리가 끝나면 📌로 바뀐다
        except discord.HTTPException:
            pass
        results: list[IngestResult] = []
        try:
            results = await self._ingest_all(src, attachments, urls, context)
        finally:
            try:
                await message.remove_reaction("⏳", self.user)
            except (discord.HTTPException, AttributeError):
                pass
        if not results:
            return 0

        new = [r for r in results if not r.duplicate]
        try:
            await message.add_reaction("📌" if new else "♻️")
        except discord.HTTPException:
            pass
        if new and not quiet:   # T1: 올린 순간부터 📌까지 (과거수집은 재지 않음)
            took = (discord.utils.utcnow() - message.created_at).total_seconds()
            if 0 <= took <= PROCESS_CAP:
                for r in new:
                    self.service.db.update_doc(r.doc["id"], process_seconds=round(took, 1))
                    r.doc["process_seconds"] = round(took, 1)
        dups = [r.doc for r in results if r.duplicate and r.doc.get("message_id") != message.id]
        if not quiet and dups:
            try:   # 다시 올린 사람이 원본을 바로 찾을 수 있게 알려 준다
                await message.reply(render_duplicates(self.service, dups), silent=True, mention_author=False,
                                    suppress_embeds=True, delete_after=self.config.prompt_ttl or None)
            except discord.HTTPException:
                log.warning("중복 안내를 보내지 못했습니다: %s", message.jump_url)
        if not quiet:
            for r in new:
                view = build_panel_view(r.doc, "p", self.config.topic_tags)
                if view:   # 확신하면 📌 반응만, 애매할 때만 말을 건다
                    try:
                        await message.reply(render_prompt(self.service, r.doc), view=view, silent=True,
                                            mention_author=False, delete_after=self.config.prompt_ttl or None)
                    except discord.HTTPException:
                        log.warning("확인 메시지를 보내지 못했습니다: %s", message.jump_url)
        return len(new)

    def start_measure(self, guild_id: int, user_id: int) -> None:
        self.measure_starts[(guild_id, user_id)] = discord.utils.utcnow()

    def finish_measure(self, interaction: discord.Interaction, shared) -> str | None:
        """측정 모드였으면 전체 시간과 구간(T0 입력 + T2 응답 + T3 고르기)을 기록하고 안내 문구를 돌려준다."""
        started = self.measure_starts.pop((interaction.guild_id, interaction.user.id), None)
        if started is None:
            return None
        now = discord.utils.utcnow()
        total = (now - started).total_seconds()
        if total > MEASURE_WINDOW:
            return None
        guild, user, doc_id = interaction.guild_id, interaction.user.id, shared.doc["id"]
        self.service.db.add_event(guild, "measure", user, doc_id, total)
        parts = []
        if shared.search_started is not None:
            typing = max((shared.search_started - started).total_seconds(), 0.0)
            self.service.db.add_event(guild, "measure_t0", user, doc_id, typing)
            parts.append(f"검색어 입력 {fmt_seconds(typing)}")
        if shared.response is not None:
            parts.append(f"봇 응답 {fmt_seconds(shared.response)}")
        if shared.pick is not None:
            parts.append(f"고르기 {fmt_seconds(shared.pick)}")
        return f"⏱ 측정 결과: 전체 {fmt_seconds(total)}" + (f" = {' + '.join(parts)}" if parts else "")

    async def _ingest_all(self, src: Source, attachments: list, urls: list[str],
                          context: list[str]) -> list[IngestResult]:
        results: list[IngestResult] = []
        for att in attachments[:MAX_ITEMS_PER_MESSAGE]:
            started = time.monotonic()
            result = await self.service.ingest_file(src, original_filename(att), await att.read(), context)
            log.info("파일 처리 %.1f초: %s", time.monotonic() - started, original_filename(att))
            if result is not None:   # 밈·짤로 판단한 이미지는 보관하지 않는다
                results.append(result)
        for url in urls[: max(MAX_ITEMS_PER_MESSAGE - len(attachments), 0)]:
            result = await self.service.ingest_link(src, url, context)
            if result is not None:   # 밈·GIF·잡담 링크는 보관하지 않는다
                results.append(result)
        return results

    async def _context(self, message: discord.Message) -> list[str]:
        lines = []
        if self.config.context_messages > 0:
            try:
                async for m in message.channel.history(limit=self.config.context_messages, before=message):
                    if m.content and not m.author.bot:
                        lines.append(f"{m.author.display_name}: {m.content[:300]}")
            except discord.HTTPException:
                pass
            lines.reverse()
        if message.content:
            lines.append(f"{message.author.display_name} (자료를 올리며): {message.content[:500]}")
        return lines


async def backfill_channel(bot: "ArchiveBot", channel, limit: int) -> int:
    """채널의 최근 메시지 limit개를 오래된 것부터 차례로 보관한다 (그래야 v1 → v2 순서로 버전이 이어진다).
    history(oldest_first=True)는 채널 맨 처음부터 가져오므로, 최신순으로 받아 뒤집는다."""
    messages = [m async for m in channel.history(limit=limit)]
    count = 0
    for message in reversed(messages):
        if message.author.bot:
            continue
        try:
            count += await bot.archive_message(message, quiet=True)
        except Exception:
            log.exception("과거 메시지 처리 실패: %s", message.jump_url)
    return count


# ── 슬래시 명령 ──────────────────────────────────────────
def register_commands(bot: ArchiveBot) -> None:
    tree, service = bot.tree, bot.service
    guild_only = app_commands.guild_only()

    @tree.command(name="찾아줘", description="기억나는 말로 팀 자료를 찾아요. 결과는 나에게만 먼저 보여요.")
    @app_commands.rename(query="질문")
    @app_commands.describe(query="예: 본선용 IR 자료, 지난주에 민수가 올린 경쟁사 분석")
    @guild_only
    async def find(interaction: discord.Interaction, query: str):
        await interaction.response.defer(ephemeral=True, thinking=True)   # 3초 제한: "찾는 중" 먼저 표시
        started = interaction.created_at   # 사용자가 명령을 보낸 시각
        event_id, hits = await service.search(interaction.guild_id, interaction.user.id, query, started_at=started)
        response = (discord.utils.utcnow() - started).total_seconds()   # T2
        service.db.set_event_value(event_id, response)
        took = f" · {fmt_seconds(response)}" if bot.config.show_timings else ""
        if not hits:
            return await interaction.followup.send(
                f"'{query}'에 맞는 자료를 못 찾았어요{took}. 다른 말로 다시 물어보거나 `/드라이브`의 자료 목록 시트를 확인해 보세요.",
                ephemeral=True)
        view = discord.ui.View(timeout=None)
        for i, hit in enumerate(hits, 1):
            view.add_item(ShareButton(hit.doc["id"], event_id, index=i,
                                      channel=getattr(interaction.channel, "name", "") or ""))
        body = "\n\n".join(render_hit(i, h, bot.config.archive_base_url) for i, h in enumerate(hits, 1))
        header = f"🔎 **{query}**" + (f" · 결과 {len(hits)}건{took}" if took else "")
        await interaction.followup.send(f"{header}\n\n{body}"[:2000], view=view, ephemeral=True,
                                        suppress_embeds=True)

    @tree.context_menu(name="자료 정보")
    @guild_only
    async def doc_info(interaction: discord.Interaction, message: discord.Message):
        docs = service.db.docs_by_message(message.id)
        if not docs:
            return await interaction.response.send_message("이 메시지에서 보관된 자료가 없어요.", ephemeral=True)
        await interaction.response.send_message(render_info(service, docs[0]), view=build_panel_view(docs[0], "i", bot.config.topic_tags),
                                                ephemeral=True, suppress_embeds=True)
        for d in docs[1:MAX_ITEMS_PER_MESSAGE]:
            await interaction.followup.send(render_info(service, d), view=build_panel_view(d, "i", bot.config.topic_tags),
                                            ephemeral=True, suppress_embeds=True)

    # 팀 용어·규칙: 코드가 아니라 말로 등록한다
    terms = app_commands.Group(name="용어", description="팀 용어 사전", guild_only=True)

    @terms.command(name="등록", description="팀에서 쓰는 말을 알려 줘요. 예: 경쟁사조사=벤치마킹")
    @app_commands.rename(entry="내용")
    @app_commands.describe(entry="용어=뜻 형식. 예: 경쟁사조사=벤치마킹")
    async def term_add(interaction: discord.Interaction, entry: str):
        term, sep, meaning = entry.partition("=")
        if not sep or not term.strip() or not meaning.strip():
            return await interaction.response.send_message(
                "`용어=뜻` 형식으로 적어 주세요. 예: `경쟁사조사=벤치마킹`", ephemeral=True)
        service.db.set_term(interaction.guild_id, term.strip(), meaning.strip(), interaction.user.display_name)
        service.refresh_index(interaction.guild_id)
        await interaction.response.send_message(
            f"기억했어요: **{term.strip()}** = {meaning.strip()}. 앞으로 분류와 검색에 반영해요.")

    @terms.command(name="목록", description="등록된 팀 용어를 봐요.")
    async def term_list(interaction: discord.Interaction):
        items = service.db.glossary(interaction.guild_id)
        text = "\n".join(f"- **{t}** = {m}" for t, m in items) or "등록된 용어가 없어요. `/용어 등록`으로 추가해 보세요."
        await interaction.response.send_message(text[:2000], ephemeral=True)

    @terms.command(name="삭제", description="팀 용어를 지워요.")
    @app_commands.rename(term="용어")
    async def term_delete(interaction: discord.Interaction, term: str):
        ok = service.db.delete_term(interaction.guild_id, term.strip())
        await interaction.response.send_message(
            f"**{term}** 용어를 지웠어요." if ok else f"'{term}'은(는) 등록된 용어가 아니에요.", ephemeral=not ok)

    rules = app_commands.Group(name="규칙", description="팀 정리 규칙", guild_only=True)

    @rules.command(name="등록", description="정리 규칙을 말로 알려 줘요. 예: 리서치 자료는 제목 앞에 [리서치]")
    @app_commands.rename(text="내용")
    async def rule_add(interaction: discord.Interaction, text: str):
        rule_id = service.db.add_rule(interaction.guild_id, text.strip(), interaction.user.display_name)
        await interaction.response.send_message(f"규칙 {rule_id}번으로 기억했어요: {text.strip()}\n다음에 올라오는 자료부터 적용해요.")

    @rules.command(name="목록", description="등록된 규칙을 봐요.")
    async def rule_list(interaction: discord.Interaction):
        service.ensure_default_rules(interaction.guild_id)
        items = service.db.rules(interaction.guild_id)
        rules_text = "\n".join(f"`{i}` {t}" for i, t in items) or "등록된 규칙이 없어요. `/규칙 등록`으로 추가해 보세요."
        tags_text = render_tags(bot.config.topic_tags, service.db.tag_counts(interaction.guild_id))
        room = 2000 - len(tags_text) - 30
        if len(rules_text) > room:
            rules_text = rules_text[: max(room, 0)] + " …"
        await interaction.response.send_message(f"**팀 규칙**\n{rules_text}\n\n{tags_text}"[:2000], ephemeral=True)

    @rules.command(name="삭제", description="규칙을 지워요.")
    @app_commands.rename(rule_id="번호")
    async def rule_delete(interaction: discord.Interaction, rule_id: int):
        ok = service.db.delete_rule(interaction.guild_id, rule_id)
        await interaction.response.send_message(
            f"규칙 {rule_id}번을 지웠어요." if ok else f"규칙 {rule_id}번이 없어요.", ephemeral=not ok)

    tree.add_command(terms)
    tree.add_command(rules)

    @tree.command(name="수집", description="이 채널의 자료 자동 보관을 켜거나 꺼요.")
    @app_commands.rename(state="상태")
    @app_commands.choices(state=[app_commands.Choice(name="켜기", value="on"), app_commands.Choice(name="끄기", value="off")])
    @app_commands.default_permissions(manage_channels=True)
    @guild_only
    async def collect(interaction: discord.Interaction, state: app_commands.Choice[str]):
        on = state.value == "on"
        service.db.set_collect(interaction.guild_id, interaction.channel_id, on)
        await interaction.response.send_message(
            "이 채널에 올라오는 파일과 링크를 보관해요." if on
            else "이 채널에서는 더 이상 자료를 읽거나 보관하지 않아요. 이미 보관된 자료는 그대로 있어요.")

    @tree.command(name="과거수집", description="이 채널의 지난 메시지에서 파일과 링크를 찾아 보관해요.")
    @app_commands.rename(limit="메시지수")
    @app_commands.default_permissions(manage_guild=True)
    @guild_only
    async def backfill(interaction: discord.Interaction, limit: app_commands.Range[int, 1, 1000] = 200):
        if not service.db.collect_enabled(interaction.guild_id, interaction.channel_id):
            return await interaction.response.send_message("이 채널은 수집이 꺼져 있어요. `/수집 켜기` 후 다시 해 주세요.", ephemeral=True)
        await interaction.response.defer(ephemeral=True, thinking=True)
        count = await backfill_channel(bot, interaction.channel, limit)
        await interaction.followup.send(f"최근 메시지 {limit}개를 훑어 자료 {count}건을 새로 보관했어요.", ephemeral=True)

    @tree.command(name="드라이브", description="자료가 쌓이는 구글 드라이브 폴더와 목록 시트 링크를 봐요.")
    @guild_only
    async def drive(interaction: discord.Interaction):
        sync = service.sync
        if sync is None:
            return await interaction.response.send_message(
                "구글 드라이브가 연결되어 있지 않아요. 봇을 실행하는 사람이 `.env`에 `GOOGLE_CREDENTIALS`를 넣으면 "
                "자료가 드라이브 폴더와 시트에 자동으로 쌓여요.", ephemeral=True)
        await interaction.response.defer(ephemeral=True, thinking=True)
        if sync.links(interaction.guild_id)["folder"] is None or sync.links(interaction.guild_id)["pending"]:
            try:
                await sync.sync_guild(interaction.guild_id)
            except Exception:
                log.exception("드라이브 동기화 실패")
        links = sync.links(interaction.guild_id)
        if links["folder"] is None:
            return await interaction.followup.send(
                "드라이브 폴더를 만들지 못했어요. 봇 실행 기록(로그)에서 구글 인증 오류를 확인해 주세요.", ephemeral=True)
        lines = [f"📁 [자료 폴더]({links['folder']})"]
        if links["sheet"]:
            lines.append(f"📊 [자료 목록 시트]({links['sheet']}) — 봇이 계속 덮어쓰니, 고칠 땐 자료 메시지 우클릭 → 앱 → **자료 정보**를 써 주세요.")
        if links["pending"]:
            lines.append(f"⏳ 아직 올리지 못한 자료 {links['pending']}건은 잠시 후 다시 올려요.")
        lines.append("이 폴더를 ChatGPT·Gemini·Claude의 구글 드라이브 연결에 추가하면 AI가 팀 자료를 바로 읽어요.")
        await interaction.followup.send("\n".join(lines), ephemeral=True, suppress_embeds=True)

    @tree.command(name="측정", description="찾기 시간 측정을 시작해요 (검증용). 찾아서 공유하면 걸린 시간을 알려 줘요.")
    @guild_only
    async def measure(interaction: discord.Interaction):
        bot.start_measure(interaction.guild_id, interaction.user.id)
        await interaction.response.send_message(
            "⏱ 측정을 시작했어요. 지금부터 `/찾아줘`로 자료를 찾아 **공유 버튼**을 누르면, "
            "검색어 입력·봇 응답·고르기에 걸린 시간을 알려 드려요. (15분 안에)", ephemeral=True)

    @tree.command(name="통계", description="자료 보관·검색 지표를 봐요.")
    @app_commands.rename(days="기간")
    @app_commands.describe(days="최근 며칠을 볼지 (기본 7일)")
    @guild_only
    async def stats(interaction: discord.Interaction, days: app_commands.Range[int, 1, 365] = 7):
        s = service.db.stats(interaction.guild_id, days)
        pct = lambda a, b: f"{a / b:.0%}" if b else "–"  # noqa: E731
        median = f"{s['median_search_seconds']:.0f}초" if s["median_search_seconds"] is not None else "–"
        await interaction.response.send_message(
            f"**최근 {days}일 지표**\n"
            f"- 보관된 자료: {s['docs']}건\n"
            f"- 재요청·누락: 재요청 메시지 {s['rerequests']}건 + 같은 자료 재업로드 {s['reshares']}건 "
            f"(자료 1건당 {pct(s['rerequests'] + s['reshares'], s['docs'])})\n"
            f"- AI 태그 수정률: 공유 의도 {pct(s['corrected'], s['docs'])} ({s['corrected']}건), "
            f"주제 태그 {pct(s['tag_corrected'], s['docs'])} ({s['tag_corrected']}건)을 사람이 고침\n"
            f"- 검색 소요 시간(중앙값): {median} — `/찾아줘`부터 공유 버튼까지\n"
            f"- 아카이브 재사용: `/찾아줘` {s['searches']}회, 공유 {s['shares']}회, "
            f"다시 쓰인 자료 {pct(s['reused'], s['docs'])}\n"
            f"{render_timing(s['timing'])}", ephemeral=True)

    @tree.error
    async def on_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
        log.exception("명령 처리 실패", exc_info=error)
        text = "처리하다 문제가 생겼어요. 잠시 후 다시 시도해 주세요."
        if interaction.response.is_done():
            await interaction.followup.send(text, ephemeral=True)
        else:
            await interaction.response.send_message(text, ephemeral=True)
