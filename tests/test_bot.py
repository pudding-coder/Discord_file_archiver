"""디스코드 연결부: 실제 서버 없이 가짜 메시지로 수집 흐름을 확인한다."""
from datetime import datetime, timezone
from types import SimpleNamespace

from archivebot.bot import ArchiveBot, build_panel_view, render_hit, render_info, render_prompt
from archivebot.service import SearchHit
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
    assert {"찾아줘", "최근", "수정", "용어", "규칙", "수집", "과거수집", "카톡가져오기", "통계", "내보내기", "자료 정보"} <= names
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
    assert ids == [f"intent:{doc['id']}:{k}:p" for k in ("reference", "competitor", "feedback", "submission")]
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
    assert build_panel_view(done, "p") is None
    assert len(build_panel_view(done, "i").children) == 4
    assert "v1 IR_v1" in render_info(service, done)
    hit = render_hit(1, SearchHit(doc=v1, latest=v2), "https://drive.example/folder")
    assert "최신본이 아니에요" in hit and "원문 메시지" in hit and "드라이브 폴더" in hit
