"""파일·링크에서 글자를 뽑는다.

- 글자가 들어 있는 PDF, TXT·MD·CSV, DOCX, PPTX, XLSX, HWPX, HWP(5.0)는 여기서 직접 읽는다.
- 스캔 PDF와 이미지는 글자가 없으므로 Claude에게 원본을 보여 주고 읽게 한다(OCR).
  여기서는 그 판단(pdf_needs_ocr)과 보낼 원본 준비(trim_pdf, prepare_image)만 한다.
- 옛 형식(DOC·PPT·XLS)은 읽지 못한다. 사본은 보관하고 파일명과 대화 맥락으로 분류한다.
"""
from __future__ import annotations

import asyncio
import io
import ipaddress
import re
import socket
import struct
import zipfile
import zlib
from dataclasses import dataclass
from html.parser import HTMLParser
from urllib.parse import urljoin, urlsplit

import aiohttp

URL_RE = re.compile(r"https?://[^\s<>()\[\]\"']+")
_TRAILING = ".,;:!?)]}>'\""
# 자료가 아닌 링크: 디스코드 내부 링크, 움짤, 이미지·영상 직링크
_SKIP_HOSTS = ("discord.com", "discord.gg", "discordapp.com", "tenor.com", "giphy.com")
_SKIP_EXT = re.compile(r"\.(png|jpe?g|gif|webp|mp4|mov|webm)(\?.*)?$", re.IGNORECASE)
TEXT_EXTS = {"txt", "md", "csv"}
IMAGE_EXTS = {"png", "jpg", "jpeg", "webp", "gif"}
MAX_TEXT_CHARS = 200_000          # 이보다 긴 본문은 잘라서 보관한다
MAX_UNZIPPED_BYTES = 50_000_000   # 압축 폭탄 방지
IMAGE_MAX_SIDE = 1568             # Claude가 권장하는 이미지 긴 변
IMAGE_MAX_BYTES = 3_500_000
MAX_LINK_BYTES = 2_000_000


def find_urls(text: str) -> list[str]:
    seen: list[str] = []
    for raw in URL_RE.findall(text or ""):
        url = raw.rstrip(_TRAILING)
        host = (urlsplit(url).hostname or "").lower()
        if any(host == h or host.endswith("." + h) for h in _SKIP_HOSTS) or _SKIP_EXT.search(url):
            continue
        if url not in seen:
            seen.append(url)
    return seen


def extension(filename: str) -> str:
    return filename.rsplit(".", 1)[-1].lower() if "." in filename else ""


def extract_file_text(filename: str, data: bytes) -> str:
    readers = {
        "pdf": _pdf_text, "docx": _docx_text, "pptx": _pptx_text, "xlsx": _xlsx_text,
        "hwpx": _hwpx_text, "hwp": _hwp_text,
    }
    ext = extension(filename)
    if ext in TEXT_EXTS:
        for enc in ("utf-8", "cp949"):
            try:
                return data.decode(enc)[:MAX_TEXT_CHARS]
            except UnicodeDecodeError:
                continue
        return ""
    reader = readers.get(ext)
    if reader is None:
        return ""
    try:
        return reader(data).strip()[:MAX_TEXT_CHARS]
    except Exception:
        return ""   # 암호화·손상·지원하지 않는 변형: 본문 없이 파일명과 맥락으로 분류


def _clean(lines) -> str:
    return "\n".join(line.strip() for line in lines if line and line.strip())


def _pdf_text(data: bytes) -> str:
    from pypdf import PdfReader

    reader = PdfReader(io.BytesIO(data))
    return "\n".join((page.extract_text() or "") for page in reader.pages).strip()


def pdf_page_count(data: bytes) -> int:
    try:
        from pypdf import PdfReader

        return len(PdfReader(io.BytesIO(data)).pages)
    except Exception:
        return 0


def pdf_needs_ocr(data: bytes, text: str) -> bool:
    """글자가 거의 없는 PDF = 스캔본이나 이미지로 만든 PDF."""
    pages = pdf_page_count(data)
    if pages == 0:
        return False   # 열리지 않는 PDF는 Claude도 못 읽는다
    return len(re.sub(r"\s+", "", text)) < 30 * pages


def trim_pdf(data: bytes, max_pages: int) -> bytes:
    """앞쪽 max_pages쪽만 남긴 PDF. 분류에는 앞부분이면 충분하고, 쪽수만큼 비용이 든다."""
    from pypdf import PdfReader, PdfWriter

    reader = PdfReader(io.BytesIO(data))
    if len(reader.pages) <= max_pages:
        return data
    writer = PdfWriter()
    for page in reader.pages[:max_pages]:
        writer.add_page(page)
    buf = io.BytesIO()
    writer.write(buf)
    return buf.getvalue()


def prepare_image(data: bytes) -> tuple[str, bytes] | None:
    """Claude에 보낼 (media_type, bytes). 너무 크면 줄이고, 열 수 없는 이미지는 None."""
    from PIL import Image

    try:
        img = Image.open(io.BytesIO(data))
        img.load()
    except Exception:
        return None
    fmt = (img.format or "").upper()
    if fmt in {"PNG", "JPEG", "WEBP", "GIF"} and max(img.size) <= IMAGE_MAX_SIDE and len(data) <= IMAGE_MAX_BYTES:
        return f"image/{'jpeg' if fmt == 'JPEG' else fmt.lower()}", data
    img.thumbnail((IMAGE_MAX_SIDE, IMAGE_MAX_SIDE))
    if img.mode in ("RGBA", "LA", "P"):
        img = img.convert("RGBA")
        background = Image.new("RGB", img.size, "white")   # 투명 배경이 검게 보이지 않게
        background.paste(img, mask=img.getchannel("A"))
        img = background
    elif img.mode != "RGB":
        img = img.convert("RGB")
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=85)
    return "image/jpeg", buf.getvalue()


def _docx_text(data: bytes) -> str:
    from docx import Document

    doc = Document(io.BytesIO(data))
    lines = [p.text for p in doc.paragraphs]
    for table in doc.tables:
        for row in table.rows:
            lines.append(" | ".join(cell.text.strip() for cell in row.cells))
    return _clean(lines)


def _pptx_text(data: bytes) -> str:
    from pptx import Presentation

    prs = Presentation(io.BytesIO(data))
    lines = []
    for no, slide in enumerate(prs.slides, 1):
        lines.append(f"[슬라이드 {no}]")
        for shape in slide.shapes:
            lines.extend(_shape_text(shape))
        if slide.has_notes_slide and slide.notes_slide.notes_text_frame is not None:
            notes = slide.notes_slide.notes_text_frame.text.strip()
            if notes:
                lines.append(f"(발표자 노트) {notes}")
    return _clean(lines)


def _shape_text(shape) -> list[str]:
    if getattr(shape, "shape_type", None) == 6:   # 그룹 도형
        return [line for child in shape.shapes for line in _shape_text(child)]
    if getattr(shape, "has_text_frame", False):
        return [shape.text_frame.text]
    if getattr(shape, "has_table", False):
        return [" | ".join(c.text.strip() for c in row.cells) for row in shape.table.rows]
    return []


def _xlsx_text(data: bytes) -> str:
    from openpyxl import load_workbook

    wb = load_workbook(io.BytesIO(data), read_only=True, data_only=True)
    lines = []
    for ws in wb.worksheets[:5]:
        lines.append(f"[시트 {ws.title}]")
        for row in ws.iter_rows(max_row=300, values_only=True):
            cells = [str(v) for v in row if v is not None and str(v).strip()]
            if cells:
                lines.append(" | ".join(cells))
    wb.close()
    return _clean(lines)


def _hwpx_text(data: bytes) -> str:
    """HWPX = 압축된 XML. Contents/section0.xml … 의 <hp:t>를 문단(<hp:p>) 단위로 모은다."""
    import xml.etree.ElementTree as ET

    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        sections = sorted(
            (n for n in zf.namelist() if re.fullmatch(r"Contents/section\d+\.xml", n)),
            key=lambda n: int(re.search(r"\d+", n.rsplit("/", 1)[1]).group()),
        )
        parts: list[str] = []
        for name in sections:
            if zf.getinfo(name).file_size > MAX_UNZIPPED_BYTES:
                continue
            root = ET.fromstring(zf.read(name))
            for elem in root.iter():
                tag = elem.tag.rsplit("}", 1)[-1]
                if tag == "p":
                    parts.append("\n")
                elif tag == "t":
                    parts.append("".join(elem.itertext()))
    return _clean("".join(parts).splitlines())


# HWP 5.0 문단 글자(HWPTAG_PARA_TEXT) 안의 제어 문자. 대부분 뒤따르는 7글자를 차지한다.
_HWP_TAG_PARA_TEXT = 67
_HWP_ONE_CHAR_CONTROLS = {0, 10, 13, 24, 25, 26, 27, 28, 29, 30, 31}


def _hwp_text(data: bytes) -> str:
    import olefile

    with olefile.OleFileIO(io.BytesIO(data)) as ole:
        flags = struct.unpack_from("<I", ole.openstream("FileHeader").read(), 36)[0]
        if flags & 0b110:
            return ""   # 암호가 걸렸거나 배포용 문서
        compressed = bool(flags & 1)
        sections = sorted(
            (e for e in ole.listdir() if len(e) == 2 and e[0] == "BodyText" and e[1].startswith("Section")),
            key=lambda e: int(e[1][7:] or 0),
        )
        out = []
        size = 0
        for entry in sections:
            raw = ole.openstream(entry).read()
            if compressed:
                d = zlib.decompressobj(-15)
                raw = d.decompress(raw, MAX_UNZIPPED_BYTES)
            size += len(raw)
            if size > MAX_UNZIPPED_BYTES:
                break
            out.append(hwp_records_text(raw))
    return _clean("\n".join(out).splitlines())


def hwp_records_text(raw: bytes) -> str:
    """BodyText 섹션의 레코드를 훑어 문단 글자만 꺼낸다."""
    parts = []
    i = 0
    while i + 4 <= len(raw):
        header = struct.unpack_from("<I", raw, i)[0]
        i += 4
        tag, size = header & 0x3FF, (header >> 20) & 0xFFF
        if size == 0xFFF:
            if i + 4 > len(raw):
                break
            size = struct.unpack_from("<I", raw, i)[0]
            i += 4
        if tag == _HWP_TAG_PARA_TEXT:
            parts.append(hwp_para_text(raw[i : i + size]))
        i += size
    return "\n".join(p for p in parts if p.strip())


def hwp_para_text(data: bytes) -> str:
    chars = struct.unpack(f"<{len(data) // 2}H", data[: len(data) // 2 * 2])
    out = []
    j = 0
    while j < len(chars):
        c = chars[j]
        if c >= 32:
            out.append(chr(c))
            j += 1
        elif c in _HWP_ONE_CHAR_CONTROLS:
            if c in (10, 13):
                out.append("\n")
            elif c in (30, 31):
                out.append(" ")
            j += 1
        else:
            if c == 9:
                out.append("\t")
            j += 8   # 인라인·확장 제어 문자는 8글자 분량
    return "".join(out).strip()


class _HTMLText(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.title = ""
        self.description = ""
        self.parts: list[str] = []
        self._skip = 0
        self._in_title = False

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style", "noscript", "svg"):
            self._skip += 1
        elif tag == "title":
            self._in_title = True
        elif tag == "meta":
            a = dict(attrs)
            key = (a.get("property") or a.get("name") or "").lower()
            if key in ("og:title",) and a.get("content") and not self.title:
                self.title = a["content"].strip()
            if key in ("og:description", "description") and a.get("content") and not self.description:
                self.description = a["content"].strip()

    def handle_endtag(self, tag):
        if tag in ("script", "style", "noscript", "svg") and self._skip:
            self._skip -= 1
        elif tag == "title":
            self._in_title = False

    def handle_data(self, data):
        if self._in_title and not self.title:
            self.title = data.strip()
        elif not self._skip and data.strip():
            self.parts.append(data.strip())


def html_to_text(html: str) -> tuple[str, str]:
    """(제목, 본문) 반환."""
    parser = _HTMLText()
    try:
        parser.feed(html)
    except Exception:
        pass
    body = re.sub(r"\s+", " ", " ".join(parser.parts)).strip()
    if parser.description and parser.description not in body:
        body = f"{parser.description}\n{body}"
    return parser.title, body


@dataclass
class LinkContent:
    url: str
    title: str = ""
    text: str = ""
    ok: bool = False


async def _is_public_host(host: str) -> bool:
    """사설·로컬 주소로의 요청을 막는다 (채팅에 올라온 임의 URL을 서버가 대신 여는 구조이므로)."""
    try:
        infos = await asyncio.get_running_loop().getaddrinfo(host, None, type=socket.SOCK_STREAM)
    except OSError:
        return False
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if not ip.is_global:
            return False
    return bool(infos)


async def fetch_link(url: str, timeout: float = 10.0) -> LinkContent:
    result = LinkContent(url=url)
    try:
        async with aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=timeout),
            headers={"User-Agent": "Mozilla/5.0 (compatible; team-archive-bot)"},
            trust_env=True,
        ) as session:
            current = url
            for _ in range(4):  # 리다이렉트는 매번 목적지를 검사하며 직접 따라간다
                parts = urlsplit(current)
                if parts.scheme not in ("http", "https") or not parts.hostname:
                    return result
                if not await _is_public_host(parts.hostname):
                    return result
                async with session.get(current, allow_redirects=False) as resp:
                    if resp.status in (301, 302, 303, 307, 308) and resp.headers.get("Location"):
                        current = urljoin(current, resp.headers["Location"])
                        continue
                    if resp.status != 200:
                        return result
                    ctype = resp.headers.get("Content-Type", "").lower()
                    raw = await resp.content.read(MAX_LINK_BYTES)
                    if "pdf" in ctype:
                        result.text = await asyncio.to_thread(extract_file_text, "link.pdf", raw)
                    elif "html" in ctype or "text" in ctype:
                        html = raw.decode(resp.charset or "utf-8", errors="replace")
                        result.title, result.text = html_to_text(html) if "html" in ctype else ("", html)
                    result.ok = True
                    return result
    except Exception:
        return result
    return result
