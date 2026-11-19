"""디스코드 연결부: 실제 서버 없이 가짜 메시지로 수집 흐름을 확인한다."""
from datetime import datetime, timezone
from types import SimpleNamespace

from archivebot.bot import ArchiveBot, build_panel_view, render_hit, render_info, render_prompt
from archivebot.service import SearchHit, Source
from tests.conftest import make_pdf


class FakeAttachment:
    def __init__(self, filename, data):
        self.filename, self._data, self.size = filename, data, len(data)

    async def read(self):
        return self._data


class FakeChannel:
    id = 10

    def __init__(self, history=()):
        self._history = list(history)

    def history(self, **kwargs):
        async def gen():
            for m in self._history:
                yield m
        return gen()


class FakeMessage:
    def __init__(self, content="", attachments=(), channel=None, mid=100):
        self.content, self.attachments, self.id = content, list(attachments), mid
        self.author = SimpleNamespace(bot=False, id=7, display_name="민수")
        self.guild = SimpleNamespace(id=1)
        self.channel = channel or FakeChannel()
        self.jump_url = f"https://discord.com/channels/1/10/{mid}"
        self.created_at = datetime(2026, 11, 16, tzinfo=timezone.utc)
        self.reactions, self.replies = [], []

    async def add_reaction(self, emoji):
        self.reactions.append(emoji)

    async def reply(self, content, **kwargs):
        self.replies.append((content, kwargs))


def test_commands_register(config, service):
    bot = ArchiveBot(config, service)
    names = {c.name for c in bot.tree.get_commands()}
    assert {"찾아줘", "최근", "수정", "용어", "규칙", "수집", "과거수집", "통계", "내보내기", "자료 정보"} <= names
    for command in bot.tree.get_commands():
        command.to_dict(bot.tree)   # 디스코드에 보낼 명령 정의가 만들어지는지


async def test_uncertain_upload_gets_pin_and_confirmation(config, service):
    bot = ArchiveBot(config, service)
    earlier = SimpleNamespace(content="경쟁사 자료 찾았어", author=SimpleNamespace(bot=False, display_name="지현"))
    msg = FakeMessage("이거야", [FakeAttachment("분석.pdf", make_pdf("x"))], FakeChannel([earlier]))
    await bot.on_message(msg)
    assert msg.reactions == ["📌"]
    content, kwargs = msg.replies[0]
    assert "경쟁사" in content and "헷갈려요" in content
    ids = [c.custom_id for c in kwargs["view"].children]
    doc = service.db.list_docs(1)[0]
    assert ids[:4] == [f"intent:{doc['id']}:{k}:p" for k in ("reference", "competitor", "feedback", "submission")]
    assert ids[4] == f"tags:{doc['id']}:p"     # 간이 분류는 태그도 확신이 없어 함께 묻는다
    assert "주제도 헷갈려요" in content
    assert kwargs["silent"] and kwargs["delete_after"] == config.prompt_ttl
    assert doc["jump_url"] == msg.jump_url and doc["author_name"] == "민수"


async def test_plain_chat_is_ignored_but_rerequest_is_counted(config, service):
    bot = ArchiveBot(config, service)
    await bot.on_message(FakeMessage("점심 뭐 먹지"))
    await bot.on_message(FakeMessage("그 IR 자료 다시 올려줘"))
    assert service.db.list_docs(1) == []
    assert service.db.stats(1, 7)["rerequests"] == 1


async def test_images_and_disabled_channels_are_skipped(config, service):
    bot = ArchiveBot(config, service)
    await bot.on_message(FakeMessage("", [FakeAttachment("screenshot.png", b"x")]))
    service.db.set_collect(1, 10, False)
    await bot.on_message(FakeMessage("", [FakeAttachment("a.pdf", make_pdf("x"))]))
    assert service.db.list_docs(1) == []


async def test_duplicate_gets_recycle_reaction_and_links_are_archived(config, service):
    bot = ArchiveBot(config, service)
    pdf = make_pdf("x")
    await bot.on_message(FakeMessage("", [FakeAttachment("a.pdf", pdf)], mid=1))
    again = FakeMessage("", [FakeAttachment("a.pdf", pdf)], mid=2)
    await bot.on_message(again)
    assert again.reactions == ["♻️"] and again.replies == []
    link = FakeMessage("참고 https://example.com/post", mid=3)
    await bot.on_message(link)
    assert link.reactions == ["📌"] and service.db.list_docs(1)[0]["kind"] == "link"


async def test_quiet_backfill_does_not_reply(config, service):
    bot = ArchiveBot(config, service)
    msg = FakeMessage("", [FakeAttachment("a.pdf", make_pdf("x"))])
    assert await bot.archive_message(msg, quiet=True) == 1
    assert msg.replies == [] and msg.reactions == ["📌"]


async def test_panels_render(config, service, src):
    v1 = (await service.ingest_file(src, "IR_v1.pdf", make_pdf("a"), [])).doc
    v2 = (await service.ingest_file(src, "IR_v2.pdf", make_pdf("b"), [])).doc
    prompt = render_prompt(service, v2)
    assert "v2" in prompt and "IR_v1" in prompt
    ids = [c.custom_id for c in build_panel_view(v2, "p").children]
    assert f"ver:{v2['id']}:keep" in ids and f"ver:{v2['id']}:split" in ids
    done = service.resolve_version(v2["id"], True, 7, "민수")
    done = service.set_intent(done["id"], "reference", 7, "민수")
    assert build_panel_view(done, "p") is not None          # 태그가 아직 남음
    done = service.set_tags(done["id"], ["발표"], 7, "민수")
    assert build_panel_view(done, "p") is None
    assert len(build_panel_view(done, "i", config.topic_tags).children) == 5   # 의도 4 + 태그 메뉴
    assert "v1 IR_v1" in render_info(service, done)
    hit = render_hit(1, SearchHit(doc=v1, latest=v2), "https://drive.example/folder")
    assert "최신본이 아니에요" in hit and "원문 메시지" in hit and "드라이브 폴더" in hit


def test_drive_link_replaces_folder_link(config, service):
    doc = {"jump_url": "https://discord.com/x", "url": None, "drive_url": "https://drive.google.com/file/d/1/view",
           "archive_path": "1/2026-11/a.pdf"}
    links = render_hit.__globals__["doc_links"](doc, "https://drive.example/folder")
    assert any("드라이브에서 열기" in l for l in links) and not any("드라이브 폴더" in l for l in links)


async def test_meme_image_gets_no_reaction(config, service):
    from tests.test_service import ScriptedAnalyzer, make_png

    service.analyzer = ScriptedAnalyzer(is_material=False)
    bot = ArchiveBot(config, service)
    msg = FakeMessage("ㅋㅋㅋ", [FakeAttachment("image.png", make_png())])
    await bot.on_message(msg)
    assert msg.reactions == [] and msg.replies == [] and service.db.list_docs(1) == []


def test_original_filename_uses_discord_title():
    from archivebot.bot import original_filename

    att = SimpleNamespace(filename="ee3efaab02cf9918.pdf", title="테스트파일")
    assert original_filename(att) == "테스트파일.pdf"
    assert original_filename(SimpleNamespace(filename="ee3e.pdf", title="보고서.pdf")) == "보고서.pdf"
    assert original_filename(SimpleNamespace(filename="MAP.md", title=None)) == "MAP.md"
    assert original_filename(SimpleNamespace(filename="MAP.md")) == "MAP.md"


async def test_korean_named_pdf_keeps_its_name(config, service):
    bot = ArchiveBot(config, service)
    att = FakeAttachment("ee3efaab02cf9918.pdf", make_pdf("x"))
    att.title = "테스트파일"
    await bot.on_message(FakeMessage("", [att]))
    doc = service.db.list_docs(1)[0]
    assert doc["filename"] == "테스트파일.pdf" and doc["title"] == "테스트파일"
    assert doc["archive_path"].endswith("_테스트파일.pdf")


async def test_markdown_file_is_archived(config, service):
    bot = ArchiveBot(config, service)
    msg = FakeMessage("지도", [FakeAttachment("MAP.md", "# 맵 꾸미기 (크리스마스 마켓)\n맵은 Studio에서 만든다.".encode())])
    await bot.on_message(msg)
    doc = service.db.list_docs(1)[0]
    assert msg.reactions == ["📌"] and "크리스마스 마켓" in doc["excerpt"]


async def test_forwarded_message_files_are_archived(config, service):
    bot = ArchiveBot(config, service)
    msg = FakeMessage("")
    msg.message_snapshots = [SimpleNamespace(
        attachments=[FakeAttachment("MAP.md", "# 지도".encode())], content="참고 https://example.com/forwarded")]
    await bot.on_message(msg)
    kinds = sorted(d["kind"] for d in service.db.list_docs(1))
    assert kinds == ["file", "link"] and msg.reactions == ["📌"]


async def test_tags_command_lists_default_and_used_tags(config, service, src):
    from archivebot.bot import render_tags
    from tests.test_service import ScriptedAnalyzer

    service.analyzer = ScriptedAnalyzer(tags=["리서치", "아이디어"])
    await service.ingest_file(src, "a.pdf", make_pdf("a"), [])
    service.analyzer = ScriptedAnalyzer(tags=["리서치"])
    await service.ingest_file(src, "b.pdf", make_pdf("b"), [])
    assert service.db.tag_counts(1) == [("리서치", 2), ("아이디어", 1)]
    text = render_tags(config.topic_tags, service.db.tag_counts(1))
    assert "기획, 리서치" in text and "리서치 2, 아이디어 1" in text and "기본 목록에 없는 태그: 아이디어" in text
    assert "아직 태그가 붙은 자료가 없어요" in render_tags(config.topic_tags, [])
    assert "태그" in {c.name for c in ArchiveBot(config, service).tree.get_commands()}


async def test_default_rules_are_seeded_once(config, service, src):
    from archivebot.config import default_rules

    await service.ingest_file(src, "a.pdf", make_pdf("a"), [])
    rules = [t for _, t in service.db.rules(1)]
    assert rules == default_rules(config.topic_tags)
    assert "리서치: 조사 자료" in rules[0] and "제출용" in rules[1]
    for rule_id, _ in service.db.rules(1):                     # 팀이 전부 지워도
        service.db.delete_rule(1, rule_id)
    await service.ingest_file(src, "b.pdf", make_pdf("b"), [])
    assert service.db.rules(1) == []                            # 다시 넣지 않는다

    config.default_rules = False
    await service.ingest_file(Source(guild_id=2, author_name="x"), "c.pdf", make_pdf("c"), [])
    assert service.db.rules(2) == []


async def test_uncertain_tags_ask_with_menu_and_confident_ones_do_not(config, service, src):
    from tests.test_service import ScriptedAnalyzer

    service.analyzer = ScriptedAnalyzer(tags=["리서치"], tag_confidence=0.4, confidence=0.95)
    doc = (await service.ingest_file(src, "a.pdf", make_pdf("a"), [])).doc
    assert doc["pending_tags"] == 1 and doc["pending_intent"] == 0
    view = build_panel_view(doc, "p", config.topic_tags)
    (menu,) = view.children                                     # 의도는 확신해서 태그 메뉴만
    assert menu.custom_id == f"tags:{doc['id']}:p" and menu.item.max_values == 3
    assert [o.label for o in menu.item.options if o.default] == ["리서치"]
    prompt = render_prompt(service, doc)
    assert "주제도 헷갈려요" in prompt and "왜 올린" not in prompt

    fixed = service.set_tags(doc["id"], ["리서치", "아이디어"], 9, "지현")
    assert fixed["tags"] == ["리서치", "아이디어"] and fixed["pending_tags"] == 0
    assert build_panel_view(fixed, "p") is None
    assert service.db.stats(1, 7)["tag_corrected"] == 1
    # 기본 목록에 없는 태그도 메뉴에 선택된 채로 보인다
    info_menu = [c for c in build_panel_view(fixed, "i", config.topic_tags).children if c.custom_id.startswith("tags:")][0]
    assert {o.label for o in info_menu.item.options if o.default} == {"리서치", "아이디어"}

    service.analyzer = ScriptedAnalyzer(tag_confidence=0.95, confidence=0.95)
    sure = (await service.ingest_file(src, "b.pdf", make_pdf("b"), [])).doc
    assert sure["pending_tags"] == 0 and build_panel_view(sure, "p") is None


async def test_tag_menu_callback_saves_selection(config, service, src):
    from archivebot.bot import TagSelect

    doc = (await service.ingest_file(src, "a.pdf", make_pdf("a"), [])).doc
    item = TagSelect(doc["id"], "p", config.topic_tags, doc["tags"])
    item.item._values = ["발표"]
    edits = []

    async def edit_message(**kwargs):
        edits.append(kwargs)

    interaction = SimpleNamespace(
        client=SimpleNamespace(service=service), user=SimpleNamespace(id=9, display_name="지현"),
        response=SimpleNamespace(edit_message=edit_message, send_message=None))
    await item.callback(interaction)
    assert service.db.get_doc(doc["id"])["tags"] == ["발표"]
    assert edits and "view" in edits[0]
