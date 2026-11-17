"""한국어에 맞춘 어휘 검색: 띄어쓰기를 무시한 글자 2-gram 겹침으로 후보를 뽑는다.

의미 기반 재정렬은 llm.py가 맡고, 여기서는 API 없이도 동작하는 1차 후보 선정과
버전 후보(비슷한 자료) 탐지를 담당한다.
"""
from __future__ import annotations

import re
from typing import Iterable

_STRIP = re.compile(r"[\W_]+", re.UNICODE)
# 버전·상태 표기: 파일명에서 지워야 "같은 자료의 다른 버전"을 알아볼 수 있다.
_VERSION_MARK = re.compile(
    r"(v(?:er)?\.?\s*\d+(?:\.\d+)*|수정\s*\d*\s*차?본?|\d+\s*차본?|최최*종|진짜|final|draft|초안|"
    r"예선(?:용|본)?|본선(?:용|본)?|복사본|copy|\(\d+\)|20\d{2}[-._]?\d{2}[-._]?\d{2}|\d{6,8})",
    re.IGNORECASE,
)


def normalize(text: str) -> str:
    return _STRIP.sub("", (text or "").lower())


def bigrams(text: str) -> set[str]:
    s = normalize(text)
    if len(s) < 2:
        return {s} if s else set()
    return {s[i : i + 2] for i in range(len(s) - 1)}


def coverage(query: str, text: str) -> float:
    """질문의 2-gram 중 text에 들어 있는 비율 (0~1)."""
    q = bigrams(query)
    if not q:
        return 0.0
    return len(q & bigrams(text)) / len(q)


def jaccard(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def expand_query(query: str, glossary: Iterable[tuple[str, str]]) -> list[str]:
    """팀 용어 사전으로 질문을 넓힌다. '벤치마킹'으로 찾아도 '경쟁사조사' 자료가 나오게."""
    variants = [query]
    nq = normalize(query)
    for term, meaning in glossary:
        nt, nm = normalize(term), normalize(meaning)
        if nt and nt in nq:
            variants.append(f"{query} {meaning}")
        elif nm and nm in nq:
            variants.append(f"{query} {term}")
    return variants


def score_doc(queries: list[str], doc: dict) -> float:
    tags = " ".join(doc.get("tags") or [])
    fields = (
        (3.0, doc.get("title") or ""),
        (2.0, f"{doc.get('filename') or ''} {tags} {doc.get('version_label') or ''}"),
        (2.0, doc.get("summary") or ""),
        (1.0, doc.get("excerpt") or ""),
    )
    best = 0.0
    for q in queries:
        s = sum(w * coverage(q, text) for w, text in fields)
        best = max(best, s)
    return best / 8.0


def rank(query: str, docs: list[dict], glossary: Iterable[tuple[str, str]] = ()) -> list[tuple[float, dict]]:
    queries = expand_query(query, glossary)
    scored = [(score_doc(queries, d), d) for d in docs]
    scored = [x for x in scored if x[0] > 0.05]
    scored.sort(key=lambda x: (x[0], x[1].get("created_at") or ""), reverse=True)
    return scored


def core_name(name: str) -> str:
    """확장자와 버전 표기를 뗀 파일명/제목의 핵심."""
    stem = re.sub(r"\.[A-Za-z0-9]{1,5}$", "", name or "")
    return normalize(_VERSION_MARK.sub(" ", stem))


def _shingles(text: str, n: int = 5, limit: int = 3000) -> set[str]:
    s = normalize(text)[:limit]
    return {s[i : i + n] for i in range(0, max(len(s) - n + 1, 0))}


def similarity(name_a: str, text_a: str, name_b: str, text_b: str) -> float:
    """두 자료가 같은 계보일 가능성 (0~1). 이름 핵심이 같거나 본문이 많이 겹치면 높다."""
    ca, cb = core_name(name_a), core_name(name_b)
    name_sim = 1.0 if ca and ca == cb else jaccard(bigrams(ca), bigrams(cb))
    text_sim = jaccard(_shingles(text_a), _shingles(text_b))
    return max(name_sim, text_sim)
