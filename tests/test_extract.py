from archivebot.extract import _is_public_host, extract_file_text, fetch_link, find_urls, html_to_text
from archivebot.llm import AnalyzeInput, build_analyze_prompt, clip


def test_find_urls_skips_noise():
    text = "이거 봐 https://example.com/a?x=1. 그리고 (https://notion.so/page) https://tenor.com/view/x " \
           "https://discord.com/channels/1/2/3 https://cdn.site.com/pic.png https://example.com/a?x=1"
    assert find_urls(text) == ["https://example.com/a?x=1", "https://notion.so/page"]


def test_html_to_text():
    title, body = html_to_text("<html><head><title>제목</title><script>x()</script></head>"
                               "<body><p>본문 <b>내용</b></p><style>p{}</style></body></html>")
    assert title == "제목" and body == "본문 내용"


def test_text_file_encodings():
    assert extract_file_text("a.txt", "한글".encode("cp949")) == "한글"
    assert extract_file_text("a.pptx", b"binary") == ""
    assert extract_file_text("broken.pdf", b"not a pdf") == ""


async def test_private_addresses_are_blocked():
    assert not await _is_public_host("127.0.0.1")
    assert not await _is_public_host("169.254.169.254")
    assert not await _is_public_host("10.0.0.5")
    assert not (await fetch_link("http://127.0.0.1:8080/admin")).ok
    assert not (await fetch_link("file:///etc/passwd")).ok


def test_prompt_marks_truncation_and_includes_team_context():
    assert "생략" in clip("가" * 20000) and len(clip("가" * 20000)) < 16000
    prompt = build_analyze_prompt(AnalyzeInput(
        kind="file", name="a.pdf", text="", context=["민수: 봐줘"], glossary=[("경쟁사조사", "벤치마킹")],
        rules=["리서치는 [리서치]"], topic_tags=["기획"],
        candidates=[{"id": 3, "title": "이전", "filename": "a_v1.pdf", "version_no": 1, "summary": "s", "excerpt": "e"}]))
    for expected in ("경쟁사조사 = 벤치마킹", "리서치는 [리서치]", "민수: 봐줘", "id=3", "본문을 읽지 못했습니다"):
        assert expected in prompt
