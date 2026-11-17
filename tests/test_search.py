from archivebot import search


def doc(**kw):
    base = {"id": 1, "title": "", "filename": "", "summary": "", "excerpt": "", "tags": [], "created_at": ""}
    return {**base, **kw}


def test_spacing_and_partial_match():
    docs = [doc(id=1, title="본선용 IR 자료"), doc(id=2, title="회의록 11월 2주차")]
    assert search.rank("본선 ir자료", docs)[0][1]["id"] == 1
    assert search.rank("IR", docs)[0][1]["id"] == 1


def test_glossary_expands_query():
    docs = [doc(id=1, title="경쟁사조사 정리"), doc(id=2, title="디자인 시안")]
    assert search.rank("벤치마킹", docs) == []
    ranked = search.rank("벤치마킹", docs, [("경쟁사조사", "벤치마킹")])
    assert ranked[0][1]["id"] == 1


def test_core_name_strips_version_marks():
    assert search.core_name("IR자료_v2_최종.pdf") == search.core_name("IR자료 수정3차.pdf") == "ir자료"
    assert search.core_name("IR자료_본선용.pdf") == search.core_name("IR자료_예선용.pdf")


def test_similarity():
    assert search.similarity("전략보고서_v1.pdf", "", "전략보고서_v2.pdf", "") == 1.0
    assert search.similarity("전략보고서.pdf", "", "회의록.pdf", "") < 0.3
