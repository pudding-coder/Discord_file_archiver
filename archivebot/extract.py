"""PDF·텍스트·링크에서 글자를 뽑는다. 스캔 PDF·이미지·PPT 본문은 MVP 범위 밖(파일명과 대화 맥락으로만 분류)."""
from __future__ import annotations

import asyncio
import io
import ipaddress
import re
import socket
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
    ext = extension(filename)
    if ext == "pdf":
        return _pdf_text(data)
    if ext in TEXT_EXTS:
        for enc in ("utf-8", "cp949"):
            try:
                return data.decode(enc)
            except UnicodeDecodeError:
                continue
    return ""


def _pdf_text(data: bytes) -> str:
    try:
        from pypdf import PdfReader

        reader = PdfReader(io.BytesIO(data))
        return "\n".join((page.extract_text() or "") for page in reader.pages).strip()
    except Exception:
        return ""  # 암호화·손상·스캔본: 본문 없이 파일명과 맥락으로 분류


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
                        result.text = await asyncio.to_thread(_pdf_text, raw)
                    elif "html" in ctype or "text" in ctype:
                        html = raw.decode(resp.charset or "utf-8", errors="replace")
                        result.title, result.text = html_to_text(html) if "html" in ctype else ("", html)
                    result.ok = True
                    return result
    except Exception:
        return result
    return result
