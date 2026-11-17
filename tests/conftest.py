import io

import pytest

from archivebot.config import Config
from archivebot.db import Database
from archivebot.extract import LinkContent
from archivebot.service import ArchiveService, Source


def make_pdf(text: str) -> bytes:
    """본문에 text가 들어간 최소 PDF."""
    from pypdf import PdfWriter
    from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

    writer = PdfWriter()
    page = writer.add_blank_page(width=300, height=300)
    font = DictionaryObject({NameObject("/Type"): NameObject("/Font"), NameObject("/Subtype"): NameObject("/Type1"),
                             NameObject("/BaseFont"): NameObject("/Helvetica")})
    page[NameObject("/Resources")] = DictionaryObject(
        {NameObject("/Font"): DictionaryObject({NameObject("/F1"): writer._add_object(font)})})
    stream = DecodedStreamObject()
    stream.set_data(f"BT /F1 12 Tf 20 200 Td ({text}) Tj ET".encode())
    page[NameObject("/Contents")] = writer._add_object(stream)
    buf = io.BytesIO()
    writer.write(buf)
    return buf.getvalue()


async def fake_fetch(url: str) -> LinkContent:
    return LinkContent(url=url, title="Notion 온보딩 사례", text="온보딩 플로우를 단계별로 분석한 글", ok=True)


@pytest.fixture
def config(tmp_path):
    return Config(data_dir=tmp_path, archive_dir=tmp_path / "archive")


@pytest.fixture
def service(config):
    svc = ArchiveService(config, Database(":memory:"), link_fetcher=fake_fetch)
    yield svc
    svc.db.close()


@pytest.fixture
def src():
    return Source(guild_id=1, channel_id=10, message_id=100, author_id=7, author_name="민수",
                  jump_url="https://discord.com/channels/1/10/100")
