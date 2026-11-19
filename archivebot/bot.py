"""디스코드 연결부: 메시지 수집, 확인 버튼, 슬래시 명령."""
from __future__ import annotations

import asyncio
import io
import logging
import re

import discord
from discord import app_commands

from .config import INTENTS, Config, intent_label
from .extract import extension, find_urls
from .kakao import parse_export
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


def render_prompt(service: ArchiveService, doc: dict) -> str:
    lines = [f"📌 {one_line(doc)} 저장했어요."]
    if doc["pending_intent"]:
        label = INTENTS[doc["intent"]][0]
        lines.append(f"왜 올린 자료인지 헷갈려요. 일단 **{label}**로 뒀는데, 다르면 눌러 주세요. (안 누르면 그대로 둬요)")
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
        f"· 의도 지정: {'사람' if doc['intent_source'] == 'user' else 'AI'}",
    ]
    chain = service.db.version_chain(doc["version_group_id"] or doc["id"])
    if len(chain) > 1:
        lines.append("버전: " + " → ".join(
            f"{'**' if d['id'] == doc['id'] else ''}v{d['version_no']} {d['title']}{'**' if d['id'] == doc['id'] else ''}"
            for d in chain))
    if doc.get("jump_url"):
        lines.append(f"[원문 메시지]({doc['jump_url']})")
    lines.append("공유 의도가 다르면 아래에서 고쳐 주세요.")
    return "\n".join(lines)


def render_hit(index: int, hit: SearchHit, base_url: str) -> str:
    doc = hit.doc
    lines = [f"**{index}.** {one_line(doc)} · {doc['created_at'][:10]} · {doc['author_name']}"]
    if hit.latest:
        lines.append(f"⚠️ 최신본이 아니에요. 최신: **v{hit.latest['version_no']} {hit.latest['title']}**")
    lines.append(f"> {hit.reason or doc['summary']}")
    lines.append(" · ".join(doc_links(doc, base_url)) or "링크 없음 (공유하면 사본 파일을 올려요)")
    return "\n".join(lines)


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


def render_tags(topic_tags: list[str], counts: list[tuple[str, int]]) -> str:
    lines = ["**기본 주제 태그** (AI가 여기서 골라요)", ", ".join(topic_tags) or "없음", ""]
    if counts:
        lines.append("**자료에 붙은 태그** (자료 수)")
        lines.append(", ".join(f"{tag} {n}" for tag, n in counts[:40]))
        extra = [tag for tag, _ in counts if tag not in topic_tags]
        if extra:
            lines.append(f"기본 목록에 없는 태그: {', '.join(extra[:20])} (팀 규칙이나 `/수정`으로 붙은 태그)")
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
            label="맞아요, 새 버전" if keep else "별개 자료예요", row=1,
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
    def __init__(self, doc_id: int, event_id: int, index: int = 0):
        super().__init__(discord.ui.Button(
            label=f"{index}번 채널에 공유" if index else "채널에 공유", emoji="📣",
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
            doc = service.record_share(self.doc_id, interaction.user.id, self.event_id)
        except LookupError as e:
            return await interaction.response.send_message(str(e), ephemeral=True)
        lines = [f"🔎 {interaction.user.mention}님이 찾은 자료: {one_line(doc)}", f"> {doc['summary']}"]
        links = doc_links(doc, client.config.archive_base_url)
        if links:
            lines.append(" · ".join(links))
        files = []
        # 디스코드 원문이 없는 자료(카톡 등)는 보관해 둔 사본을 직접 올린다
        if not doc.get("jump_url") and not doc.get("drive_url") and doc["kind"] == "file" \
                and (path := service.archive_file(doc)):
            if path.stat().st_size <= 10 * 1024 * 1024:
                files.append(discord.File(path, filename=doc["filename"] or path.name))
        try:
            await interaction.channel.send("\n".join(lines), files=files, suppress_embeds=True)  # type: ignore[union-attr]
        except discord.Forbidden:
            return await interaction.response.send_message(
                "이 채널에 메시지를 보낼 권한이 없어요. 봇에 '메시지 보내기' 권한을 주세요.", ephemeral=True)
        await interaction.response.send_message("채널에 공유했어요.", ephemeral=True)


def build_panel_view(doc: dict, mode: str) -> discord.ui.View | None:
    """mode p: 자료를 올린 직후의 확인 메시지(애매한 것만 묻는다) / i: '자료 정보' 패널(항상 수정 가능)."""
    view = discord.ui.View(timeout=None)
    if mode == "i" or doc["pending_intent"]:
        for intent in INTENTS:
            view.add_item(IntentButton(doc["id"], intent, mode, selected=intent == doc["intent"]))
    if doc["pending_version"]:
        view.add_item(VersionButton(doc["id"], "keep"))
        view.add_item(VersionButton(doc["id"], "split"))
    return view if view.children else None


async def _refresh_panel(interaction: discord.Interaction, service: ArchiveService, doc: dict, mode: str) -> None:
    if mode == "i":
        return await interaction.response.edit_message(content=render_info(service, doc), view=build_panel_view(doc, "i"))
    view = build_panel_view(doc, "p")
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
        register_commands(self)

    async def setup_hook(self) -> None:
        self.add_dynamic_items(IntentButton, VersionButton, ShareButton)
        if self.service.sync is not None:
            self.service.sync.guild_name = lambda gid: getattr(self.get_guild(gid), "name", None) or str(gid)
            self._drive_task = asyncio.create_task(self.service.sync.run_forever())
        if self.config.guild_ids:
            for gid in self.config.guild_ids:
                guild = discord.Object(id=gid)
                self.tree.copy_global_to(guild=guild)
                await self.tree.sync(guild=guild)
        else:
            await self.tree.sync()

    async def on_ready(self) -> None:
        log.info("로그인: %s (서버 %d곳)", self.user, len(self.guilds))

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
        results: list[IngestResult] = []
        for att in attachments[:MAX_ITEMS_PER_MESSAGE]:
            result = await self.service.ingest_file(src, original_filename(att), await att.read(), context)
            if result is not None:   # 밈·짤로 판단한 이미지는 보관하지 않는다
                results.append(result)
        for url in urls[: max(MAX_ITEMS_PER_MESSAGE - len(attachments), 0)]:
            results.append(await self.service.ingest_link(src, url, context))
        if not results:
            return 0

        new = [r for r in results if not r.duplicate]
        try:
            await message.add_reaction("📌" if new else "♻️")
        except discord.HTTPException:
            pass
        if not quiet:
            for r in new:
                view = build_panel_view(r.doc, "p")
                if view:   # 확신하면 📌 반응만, 애매할 때만 말을 건다
                    try:
                        await message.reply(render_prompt(self.service, r.doc), view=view, silent=True,
                                            mention_author=False, delete_after=self.config.prompt_ttl or None)
                    except discord.HTTPException:
                        log.warning("확인 메시지를 보내지 못했습니다: %s", message.jump_url)
        return len(new)

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
        event_id, hits = await service.search(interaction.guild_id, interaction.user.id, query)
        if not hits:
            return await interaction.followup.send(
                f"'{query}'에 맞는 자료를 못 찾았어요. 다른 말로 다시 물어보거나 `/최근`으로 목록을 확인해 보세요.",
                ephemeral=True)
        view = discord.ui.View(timeout=None)
        for i, hit in enumerate(hits, 1):
            view.add_item(ShareButton(hit.doc["id"], event_id, index=i))
        body = "\n\n".join(render_hit(i, h, bot.config.archive_base_url) for i, h in enumerate(hits, 1))
        await interaction.followup.send(f"🔎 **{query}**\n\n{body}"[:2000], view=view, ephemeral=True,
                                        suppress_embeds=True)

    @tree.command(name="최근", description="최근에 보관된 자료 목록을 봐요.")
    @app_commands.rename(count="개수")
    @guild_only
    async def recent(interaction: discord.Interaction, count: app_commands.Range[int, 1, 20] = 10):
        docs = service.db.list_docs(interaction.guild_id, limit=count)
        if not docs:
            return await interaction.response.send_message(
                "아직 보관된 자료가 없어요. 채널에 PDF나 링크를 올리면 자동으로 보관해요.", ephemeral=True)
        lines = [f"`#{d['id']}` {one_line(d)} · {d['created_at'][:10]}"
                 + (f" · [원문]({d['jump_url']})" if d.get("jump_url") else "") for d in docs]
        await interaction.response.send_message("\n".join(lines)[:2000], ephemeral=True, suppress_embeds=True)

    @tree.command(name="수정", description="자료의 공유 의도·제목·태그를 고쳐요. 수정은 모두 기록돼요.")
    @app_commands.rename(doc_id="자료번호", intent="의도", title="제목", tags="태그")
    @app_commands.describe(doc_id="/최근이나 자료 정보에 나오는 # 번호", tags="쉼표로 구분")
    @app_commands.choices(intent=[app_commands.Choice(name=v[0], value=k) for k, v in INTENTS.items()])
    @guild_only
    async def edit(interaction: discord.Interaction, doc_id: int, intent: app_commands.Choice[str] | None = None,
                   title: str | None = None, tags: str | None = None):
        doc = service.db.get_doc(doc_id)
        if doc is None or doc["guild_id"] != interaction.guild_id:
            return await interaction.response.send_message(f"자료 #{doc_id}를 찾을 수 없어요.", ephemeral=True)
        who = (interaction.user.id, interaction.user.display_name)
        if intent:
            doc = service.set_intent(doc_id, intent.value, *who)
        if title or tags is not None:
            tag_list = [t.strip() for t in tags.split(",") if t.strip()] if tags is not None else None
            doc = service.edit_doc(doc_id, *who, title=title, tags=tag_list)
        await interaction.response.send_message(render_info(service, doc), view=build_panel_view(doc, "i"),
                                                ephemeral=True, suppress_embeds=True)

    @tree.context_menu(name="자료 정보")
    @guild_only
    async def doc_info(interaction: discord.Interaction, message: discord.Message):
        docs = service.db.docs_by_message(message.id)
        if not docs:
            return await interaction.response.send_message("이 메시지에서 보관된 자료가 없어요.", ephemeral=True)
        await interaction.response.send_message(render_info(service, docs[0]), view=build_panel_view(docs[0], "i"),
                                                ephemeral=True, suppress_embeds=True)
        for d in docs[1:MAX_ITEMS_PER_MESSAGE]:
            await interaction.followup.send(render_info(service, d), view=build_panel_view(d, "i"),
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
        items = service.db.rules(interaction.guild_id)
        text = "\n".join(f"`{i}` {t}" for i, t in items) or "등록된 규칙이 없어요. `/규칙 등록`으로 추가해 보세요."
        await interaction.response.send_message(text[:2000], ephemeral=True)

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
        count = 0
        async for message in interaction.channel.history(limit=limit, oldest_first=True):  # type: ignore[union-attr]
            if message.author.bot:
                continue
            try:
                count += await bot.archive_message(message, quiet=True)
            except Exception:
                log.exception("과거 메시지 처리 실패: %s", message.jump_url)
        await interaction.followup.send(f"최근 메시지 {limit}개를 훑어 자료 {count}건을 새로 보관했어요.", ephemeral=True)

    @tree.command(name="카톡가져오기", description="카카오톡 '대화 내보내기' 텍스트 파일에서 링크를 가져와요.")
    @app_commands.rename(file="파일")
    @app_commands.describe(file="카카오톡 대화 내보내기로 만든 .txt 파일")
    @guild_only
    async def kakao_import(interaction: discord.Interaction, file: discord.Attachment):
        if extension(file.filename) != "txt" or file.size > 5 * 1024 * 1024:
            return await interaction.response.send_message("5MB 이하의 .txt 내보내기 파일을 올려 주세요.", ephemeral=True)
        await interaction.response.defer(ephemeral=True, thinking=True)
        raw = await file.read()
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            text = raw.decode("cp949", errors="replace")
        links = parse_export(text)
        new = 0
        for link in links[:50]:
            src = Source(guild_id=interaction.guild_id, channel_id=interaction.channel_id, author_name=link.author,
                         platform="kakao", created_at=f"{link.date}T00:00:00+00:00" if link.date else None)
            result = await service.ingest_link(src, link.url, [f"{link.author} (카톡): {link.message}"])
            new += not result.duplicate
        skipped = f" (링크가 많아 앞의 50개만 처리했어요. 전체 {len(links)}개)" if len(links) > 50 else ""
        await interaction.followup.send(
            f"카톡 대화에서 링크 {min(len(links), 50)}개를 찾아 {new}건을 새로 보관했어요.{skipped}\n"
            "내보내기 파일에는 첨부파일이 들어 있지 않아 링크만 가져올 수 있어요.", ephemeral=True)

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
            lines.append(f"📊 [자료 목록 시트]({links['sheet']}) — 봇이 계속 덮어쓰니 수정은 `/수정`으로 해 주세요.")
        if links["pending"]:
            lines.append(f"⏳ 아직 올리지 못한 자료 {links['pending']}건은 잠시 후 다시 올려요.")
        lines.append("이 폴더를 ChatGPT·Gemini·Claude의 구글 드라이브 연결에 추가하면 AI가 팀 자료를 바로 읽어요.")
        await interaction.followup.send("\n".join(lines), ephemeral=True, suppress_embeds=True)

    @tree.command(name="태그", description="기본 주제 태그와 자료에 실제로 붙은 태그를 봐요.")
    @guild_only
    async def tags(interaction: discord.Interaction):
        await interaction.response.send_message(render_tags(bot.config.topic_tags,
                                                            service.db.tag_counts(interaction.guild_id)),
                                                ephemeral=True)

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
            f"- AI 태그 수정률: {pct(s['corrected'], s['docs'])} ({s['corrected']}건을 사람이 고침)\n"
            f"- 검색 소요 시간(중앙값): {median} — `/찾아줘`부터 공유 버튼까지\n"
            f"- 아카이브 재사용: `/찾아줘` {s['searches']}회, 공유 {s['shares']}회, "
            f"다시 쓰인 자료 {pct(s['reused'], s['docs'])}", ephemeral=True)

    @tree.command(name="내보내기", description="보관된 자료 목록을 CSV로 받아요.")
    @guild_only
    async def export(interaction: discord.Interaction):
        data = service.export_csv(interaction.guild_id).encode("utf-8-sig")
        await interaction.response.send_message(
            "자료 목록이에요. 엑셀이나 구글 시트에서 바로 열 수 있어요.",
            file=discord.File(io.BytesIO(data), filename="team-archive.csv"), ephemeral=True)

    @tree.error
    async def on_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
        log.exception("명령 처리 실패", exc_info=error)
        text = "처리하다 문제가 생겼어요. 잠시 후 다시 시도해 주세요."
        if interaction.response.is_done():
            await interaction.followup.send(text, ephemeral=True)
        else:
            await interaction.response.send_message(text, ephemeral=True)
