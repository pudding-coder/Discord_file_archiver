"""PPTX·DOCX·XLSX·HWPX·HWP 본문 추출과 OCR 준비."""
import io
import struct
import zipfile

from archivebot.extract import (extract_file_text, hwp_para_text, hwp_records_text, pdf_needs_ocr,
                                prepare_image, trim_pdf)
from archivebot.llm import AnalyzeInput, build_analyze_content
from tests.conftest import make_pdf
from tests.test_service import blank_pdf, make_png


def test_docx():
    from docx import Document

    d = Document()
    d.add_paragraph("본선 IR 전략")
    t = d.add_table(rows=1, cols=2)
    t.cell(0, 0).text, t.cell(0, 1).text = "매출", "3억"
    buf = io.BytesIO()
    d.save(buf)
    text = extract_file_text("a.docx", buf.getvalue())
    assert "본선 IR 전략" in text and "매출 | 3억" in text


def test_pptx_with_notes_and_table():
    from pptx import Presentation
    from pptx.util import Inches

    prs = Presentation()
    slide = prs.slides.add_slide(prs.slide_layouts[1])
    slide.shapes.title.text = "경쟁사 분석"
    slide.placeholders[1].text = "가격 비교"
    table = slide.shapes.add_table(1, 2, Inches(1), Inches(4), Inches(4), Inches(1)).table
    table.cell(0, 0).text, table.cell(0, 1).text = "A사", "월 9,900원"
    slide.notes_slide.notes_text_frame.text = "여기서 강조"
    buf = io.BytesIO()
    prs.save(buf)
    text = extract_file_text("deck.pptx", buf.getvalue())
    for expected in ("[슬라이드 1]", "경쟁사 분석", "가격 비교", "A사 | 월 9,900원", "(발표자 노트) 여기서 강조"):
        assert expected in text


def test_xlsx():
    from openpyxl import Workbook

    wb = Workbook()
    wb.active.title = "예산"
    wb.active.append(["항목", "금액"])
    wb.active.append(["서버", 50000])
    buf = io.BytesIO()
    wb.save(buf)
    text = extract_file_text("b.xlsx", buf.getvalue())
    assert "[시트 예산]" in text and "서버 | 50000" in text


def test_hwpx():
    ns = 'xmlns:hp="http://www.hancom.co.kr/hwpml/2011/paragraph"'
    section = (f'<hs:sec xmlns:hs="http://www.hancom.co.kr/hwpml/2011/section" {ns}>'
               '<hp:p><hp:run><hp:t>첫 문단</hp:t></hp:run></hp:p>'
               '<hp:p><hp:run><hp:t>둘째 <hp:tab/>문단</hp:t></hp:run></hp:p></hs:sec>')
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("mimetype", "application/hwp+zip")
        zf.writestr("Contents/section1.xml", section.replace("첫", "셋째").replace("둘째", "넷째"))
        zf.writestr("Contents/section0.xml", section)
    text = extract_file_text("c.hwpx", buf.getvalue())
    assert text.splitlines() == ["첫 문단", "둘째 문단", "셋째 문단", "넷째 문단"]


def _record(tag: int, payload: bytes) -> bytes:
    size = len(payload)
    if size >= 0xFFF:
        return struct.pack("<II", tag | (0xFFF << 20), size) + payload
    return struct.pack("<I", tag | (size << 20)) + payload


def test_hwp_records():
    # 탭(인라인 제어, 8글자 분량)과 문단 끝(13)이 섞인 문단 글자
    tab = struct.pack("<8H", 9, 0, 0, 0, 0, 0, 0, 9)
    para = "본선".encode("utf-16-le") + tab + "자료".encode("utf-16-le") + struct.pack("<H", 13)
    long_para = ("가" * 3000).encode("utf-16-le")   # 확장 크기 레코드
    raw = _record(66, b"\x00" * 10) + _record(67, para) + _record(67, long_para)
    assert hwp_para_text(para) == "본선\t자료"
    lines = hwp_records_text(raw).splitlines()
    assert lines[0] == "본선\t자료" and lines[1] == "가" * 3000
    assert hwp_records_text(b"\x43\x00") == ""   # 잘린 데이터


def test_broken_office_files_return_empty():
    for name in ("x.docx", "x.pptx", "x.xlsx", "x.hwpx", "x.hwp", "x.doc"):
        assert extract_file_text(name, b"garbage") == ""


def test_ocr_detection_and_trimming():
    assert pdf_needs_ocr(blank_pdf(3), "")
    assert not pdf_needs_ocr(make_pdf("x"), "충분히 긴 본문 " * 10)
    assert not pdf_needs_ocr(b"not a pdf", "")
    from archivebot.extract import pdf_page_count
    assert pdf_page_count(trim_pdf(blank_pdf(5), 3)) == 3
    small = blank_pdf(2)
    assert trim_pdf(small, 3) == small


def test_prepare_image():
    from PIL import Image

    small = make_png()
    assert prepare_image(small) == ("image/png", small)
    big = io.BytesIO()
    Image.new("RGBA", (4000, 1000), (0, 0, 0, 0)).save(big, "PNG")
    media_type, data = prepare_image(big.getvalue())
    img = Image.open(io.BytesIO(data))
    assert media_type == "image/jpeg" and max(img.size) == 1568
    assert img.getpixel((10, 10)) == (255, 255, 255)   # 투명 배경은 흰색으로
    assert prepare_image(b"nope") is None


def test_claude_content_puts_attachment_first():
    item = AnalyzeInput(kind="file", name="scan.pdf", text="", attachment=("application/pdf", b"%PDF"), topic_tags=["기획"])
    content = build_analyze_content(item)
    assert content[0] == {"type": "document", "source": {"type": "base64", "media_type": "application/pdf", "data": "JVBERg=="}}
    assert content[1]["type"] == "text" and "스캔 PDF" in content[1]["text"]
    img = build_analyze_content(AnalyzeInput(kind="file", name="a.png", text="", attachment=("image/png", b"x")))
    assert img[0]["type"] == "image" and "이미지" in img[1]["text"]
    plain = build_analyze_content(AnalyzeInput(kind="file", name="a.txt", text="본문"))
    assert len(plain) == 1 and "본문" in plain[0]["text"]
