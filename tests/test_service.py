import pytest

from archivebot.llm import Analysis
from archivebot.service import Source
from tests.conftest import make_pdf


class ScriptedAnalyzer:
    """정해 둔 답을 돌려주는 가짜 AI. 받은 입력을 기록한다."""
    semantic = True
    vision = True

    def __init__(self, **overrides):
        self.overrides = overrides
        self.inputs = []
        self.picks = []

    async def analyze(self, item):
        self.inputs.append(item)
        base = dict(title=item.name, summary="요약", tags=["기획"], intent="reference", confidence=0.9,
                    version_label="", previous_version_id=0, change_note="", text_excerpt="", is_material=True)
        over = self.overrides(item) if callable(self.overrides) else self.overrides
        return Analysis(**{**base, **over})

    async def pick(self, query, docs, glossary):
        self.seen_docs = docs
        return self.picks


async def test_file_is_copied_and_classified(service, src, config):
    data = make_pdf("Competitor pricing analysis")
    result = await service.ingest_file(src, "경쟁사 가격 분석.pdf", data, ["민수: 경쟁사 자료야"])
    doc = result.doc
    assert not result.duplicate
    assert (config.archive_dir / doc["archive_path"]).read_bytes() == data     # 사본 보관
    assert "Competitor pricing" in doc["excerpt"]                              # PDF 본문 추출
    assert doc["intent"] == "competitor"
    assert doc["pending_intent"] == 1          # 간이 분류는 확신이 낮아 확인을 요청
    assert doc["version_group_id"] == doc["id"] and doc["version_no"] == 1
    index = (config.archive_dir / "1" / "INDEX.md").read_text(encoding="utf-8")
    assert "경쟁사 가격 분석" in index


async def test_duplicate_is_not_stored_twice(service, src):
    data = make_pdf("same")
    first = await service.ingest_file(src, "a.pdf", data, [])
    again = await service.ingest_file(src, "a.pdf", data, [])
    assert again.duplicate and again.doc["id"] == first.doc["id"]
    assert len(service.db.list_docs(1)) == 1
    assert service.db.stats(1, 7)["reshares"] == 1


async def test_version_lineage_and_unlink(service, src):
    v1 = (await service.ingest_file(src, "IR자료_v1.pdf", make_pdf("first draft"), [])).doc
    r2 = await service.ingest_file(src, "IR자료_v2.pdf", make_pdf("second draft with numbers"), [])
    v2 = r2.doc
    assert r2.previous["id"] == v1["id"]
    assert (v2["version_group_id"], v2["version_no"], v2["pending_version"]) == (v1["id"], 2, 1)
    v3 = (await service.ingest_file(src, "IR자료_최종.pdf", make_pdf("final"), [])).doc
    assert v3["version_no"] == 3
    assert service.newer_version(v1)["id"] == v3["id"]

    split = service.resolve_version(v3["id"], keep=False, actor_id=7, actor_name="민수")
    assert (split["version_group_id"], split["version_no"], split["pending_version"]) == (v3["id"], 1, 0)
    assert service.db.audit_for(v3["id"])[-1]["action"] == "version_unlink"
    kept = service.resolve_version(v2["id"], keep=True, actor_id=7, actor_name="민수")
    assert kept["version_no"] == 2 and kept["pending_version"] == 0


async def test_unrelated_file_is_not_linked(service, src):
    await service.ingest_file(src, "IR자료_v1.pdf", make_pdf("pitch"), [])
    other = await service.ingest_file(src, "회의록.pdf", make_pdf("meeting notes about lunch"), [])
    assert other.previous is None and other.doc["version_no"] == 1


async def test_ai_cannot_link_to_non_candidate(service, src):
    service.analyzer = ScriptedAnalyzer(previous_version_id=999)
    result = await service.ingest_file(src, "a.pdf", make_pdf("x"), [])
    assert result.previous is None


async def test_confident_ai_needs_no_confirmation_and_gets_team_context(service, src):
    service.db.set_term(1, "경쟁사조사", "벤치마킹", "민수")
    service.db.add_rule(1, "리서치 자료는 제목 앞에 [리서치]", "민수")
    service.analyzer = ScriptedAnalyzer(confidence=0.95, intent="submission")
    doc = (await service.ingest_file(src, "발표.pdf", make_pdf("deck"), ["지현: 이걸로 제출하자"])).doc
    assert doc["pending_intent"] == 0 and doc["intent"] == "submission"
    item = service.analyzer.inputs[0]
    assert item.glossary == [("경쟁사조사", "벤치마킹")]
    assert item.rules == ["리서치 자료는 제목 앞에 [리서치]"]
    assert item.context == ["지현: 이걸로 제출하자"]


async def test_ai_failure_falls_back(service, src):
    class Broken:
        async def analyze(self, item):
            raise RuntimeError("API down")

    service.analyzer = Broken()
    doc = (await service.ingest_file(src, "피드백 부탁.pdf", make_pdf("x"), [])).doc
    assert doc["intent"] == "feedback" and doc["pending_intent"] == 1


async def test_set_intent_is_audited_and_counted(service, src):
    doc = (await service.ingest_file(src, "자료.pdf", make_pdf("x"), [])).doc
    updated = service.set_intent(doc["id"], "submission", 9, "지현")
    assert (updated["intent"], updated["intent_source"], updated["pending_intent"]) == ("submission", "user", 0)
    log = service.db.audit_for(doc["id"])[0]
    assert (log["actor_name"], log["before"], log["after"]) == ("지현", "reference", "submission")
    assert service.db.stats(1, 7)["corrected"] == 1
    with pytest.raises(ValueError):
        service.set_intent(doc["id"], "nonsense", 9, "지현")


async def test_link_ingest(service, src):
    doc = (await service.ingest_link(src, "https://example.com/post", ["민수: 참고해"])).doc
    assert doc["kind"] == "link" and doc["url"] == "https://example.com/post"
    assert doc["title"] == "Notion 온보딩 사례"
    assert (await service.ingest_link(src, "https://example.com/post", [])).duplicate


async def test_search_lexical_mode_and_share_metrics(service, src):
    await service.ingest_file(src, "본선용 IR 자료.pdf", make_pdf("pitch"), [])
    await service.ingest_file(src, "회의록.pdf", make_pdf("lunch"), [])
    event_id, hits = await service.search(1, 7, "본선 ir")
    assert [h.doc["title"] for h in hits] == ["본선용 IR 자료"]
    _, none = await service.search(1, 7, "전혀상관없는질문")
    assert none == []
    service.record_share(hits[0].doc["id"], 7, event_id)
    s = service.db.stats(1, 7)
    assert (s["searches"], s["shares"], s["reused"]) == (2, 1, 1)
    assert s["median_search_seconds"] is not None


async def test_search_semantic_mode_sees_whole_catalog_and_flags_old_version(service, src):
    v1 = (await service.ingest_file(src, "전략_v1.pdf", make_pdf("a"), [])).doc
    v2 = (await service.ingest_file(src, "전략_v2.pdf", make_pdf("b"), [])).doc
    ai = ScriptedAnalyzer()
    ai.picks = [(v1["id"], "토론 내용이 담긴 초안"), (12345, "없는 자료")]
    service.analyzer = ai
    _, hits = await service.search(1, 7, "언제 토론했더라")    # 글자가 하나도 안 겹치는 질문
    assert {d["id"] for d in ai.seen_docs} == {v1["id"], v2["id"]}
    assert len(hits) == 1 and hits[0].latest["id"] == v2["id"]


async def test_guilds_are_isolated(service, src):
    await service.ingest_file(src, "a.pdf", make_pdf("x"), [])
    other = Source(guild_id=2, author_name="다른팀")
    assert not (await service.ingest_file(other, "a.pdf", make_pdf("x"), [])).duplicate
    assert len(service.db.list_docs(1)) == len(service.db.list_docs(2)) == 1


async def test_export_csv(service, src):
    await service.ingest_file(src, "자료.pdf", make_pdf("x"), [])
    rows = service.export_csv(1).splitlines()
    assert rows[0].startswith("id,날짜,제목") and "자료" in rows[1]


# ── OCR (스캔 PDF·이미지) ─────────────────────────────────
def make_png(size=(40, 30), color=(200, 30, 30)) -> bytes:
    import io

    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", size, color).save(buf, "PNG")
    return buf.getvalue()


def blank_pdf(pages: int) -> bytes:
    import io

    from pypdf import PdfWriter

    w = PdfWriter()
    for _ in range(pages):
        w.add_blank_page(width=200, height=200)
    buf = io.BytesIO()
    w.write(buf)
    return buf.getvalue()


async def test_scanned_pdf_is_sent_to_ai_and_excerpt_is_searchable(service, src):
    ai = ScriptedAnalyzer(text_excerpt="2026 본선 발표 자료 매출 계획")
    service.analyzer = ai
    service.config.ocr_max_pages = 2
    doc = (await service.ingest_file(src, "scan.pdf", blank_pdf(5), [])).doc
    media_type, sent = ai.inputs[0].attachment
    from archivebot.extract import pdf_page_count
    assert media_type == "application/pdf" and pdf_page_count(sent) == 2     # 앞 2쪽만 보냄
    assert doc["excerpt"] == "2026 본선 발표 자료 매출 계획"
    from archivebot.llm import HeuristicAnalyzer
    service.analyzer = HeuristicAnalyzer()     # 글자 검색만으로도 AI가 읽은 본문이 찾아지는지
    _, hits = await service.search(1, 7, "매출 계획")
    assert hits and hits[0].doc["id"] == doc["id"]


async def test_text_pdf_is_not_sent_as_attachment(service, src):
    ai = ScriptedAnalyzer()
    service.analyzer = ai
    await service.ingest_file(src, "a.pdf", make_pdf("This PDF already has plenty of real text inside it"), [])
    assert ai.inputs[0].attachment is None


async def test_image_material_is_archived_and_meme_is_not(service, src):
    service.analyzer = ScriptedAnalyzer(text_excerpt="경쟁사 요금표", is_material=True)
    doc = (await service.ingest_file(src, "image.png", make_png(), [])).doc
    assert doc["excerpt"] == "경쟁사 요금표"
    assert service.analyzer.inputs[0].attachment[0] == "image/png"
    assert service.analyzer.inputs[0].candidates == []          # 이미지는 이름으로 버전을 찾지 않음

    service.analyzer = ScriptedAnalyzer(is_material=False)
    assert await service.ingest_file(src, "image.png", make_png(color=(1, 2, 3)), []) is None
    assert len(service.db.list_docs(1)) == 1


async def test_images_are_skipped_without_vision_or_when_ai_fails(service, src):
    assert await service.ingest_file(src, "photo.jpg", make_png(), []) is None   # 간이 분류: 이미지 못 읽음

    class Broken(ScriptedAnalyzer):
        async def analyze(self, item):
            raise RuntimeError("down")

    service.analyzer = Broken()
    assert await service.ingest_file(src, "photo.png", make_png(), []) is None
    assert await service.ingest_file(src, "broken.png", b"not an image", []) is None
    assert service.db.list_docs(1) == []
