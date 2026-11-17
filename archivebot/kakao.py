"""카카오톡 '대화 내보내기' 텍스트에서 링크를 뽑는다 (보조 입구).

단톡방을 읽는 공식 API가 없으므로, 주 1회 정도 내보낸 .txt를 디스코드에 올리면
그 안의 링크를 같은 아카이브에 넣는다. 내보내기 텍스트에는 파일 자체가 들어 있지 않아
첨부파일은 가져올 수 없다.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from .extract import find_urls

# PC:      [홍길동] [오후 3:12] 메시지
# 모바일:  2026년 11월 16일 오후 3:12, 홍길동 : 메시지
# 날짜줄:  --------------- 2026년 11월 16일 월요일 ---------------
_PC = re.compile(r"^\[(?P<name>[^\]]+)\] \[(?P<time>[^\]]+)\] (?P<body>.*)$")
_MOBILE = re.compile(
    r"^(?P<y>\d{4})년 (?P<m>\d{1,2})월 (?P<d>\d{1,2})일 (?P<time>(?:오전|오후) \d{1,2}:\d{2}), "
    r"(?P<name>[^:]+?) : (?P<body>.*)$"
)
_DATE = re.compile(r"(?P<y>\d{4})년 (?P<m>\d{1,2})월 (?P<d>\d{1,2})일")


@dataclass
class KakaoLink:
    url: str
    author: str
    date: str | None      # YYYY-MM-DD
    message: str


def parse_export(text: str) -> list[KakaoLink]:
    links: list[KakaoLink] = []
    seen: set[str] = set()
    date: str | None = None
    author, body_lines = "", []

    def flush() -> None:
        body = "\n".join(body_lines).strip()
        for url in find_urls(body):
            if url not in seen:
                seen.add(url)
                links.append(KakaoLink(url=url, author=author or "카톡", date=date, message=body[:500]))

    for line in text.splitlines():
        line = line.lstrip("﻿")
        if m := _MOBILE.match(line):
            flush()
            date = f"{m['y']}-{int(m['m']):02d}-{int(m['d']):02d}"
            author, body_lines = m["name"].strip(), [m["body"]]
        elif m := _PC.match(line):
            flush()
            author, body_lines = m["name"].strip(), [m["body"]]
        elif (m := _DATE.search(line)) and (line.startswith("-") or line.strip() == m.group(0) or "요일" in line):
            flush()
            author, body_lines = "", []
            date = f"{m['y']}-{int(m['m']):02d}-{int(m['d']):02d}"
        else:
            body_lines.append(line)   # 여러 줄 메시지의 이어지는 줄
    flush()
    return links
