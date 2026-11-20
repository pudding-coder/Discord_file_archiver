"""구글 드라이브·시트 연동: 실제 구글 대신 호출을 기록하는 가짜 클라이언트로 확인한다."""
import itertools
import json
import sqlite3

import pytest

from archivebot.db import Database
from archivebot.gdrive import GoogleSync, load_credentials, read_credentials_info
from tests.conftest import make_pdf


class Call:
    def __init__(self, fn):
        self.fn = fn

    def execute(self, num_retries=0):
        assert num_retries > 0          # 연결 끊김에 대비해 항상 다시 시도하게 부른다
        return self.fn()


class FakeHttpError(Exception):
    def __init__(self, status):
        super().__init__(f"HTTP {status}")
        self.resp = type("R", (), {"status": status})()


class FakeGoogle:
    """drive.files() / sheets.spreadsheets() 체인을 흉내 낸다."""

    def __init__(self):
        self.ids = (f"id{i}" for i in itertools.count(1))
        self.store: dict[str, dict] = {}
        self.sheet_values: dict[str, list] = {}
        self.fail_upload: Exception | None = None
        self.calls: list[str] = []

    # drive
    def files(self):
        return self

    def create(self, body, fields, supportsAllDrives, media_body=None):
        def run():
            if media_body is not None and self.fail_upload:
                raise self.fail_upload
            parent = (body.get("parents") or [None])[0]
            if parent and parent not in self.store:
                raise FakeHttpError(404)
            fid = next(self.ids)
            self.store[fid] = {**body, "media": media_body}
            self.calls.append(f"create:{body.get('mimeType', 'file')}:{body['name']}")
            return {"id": fid, "webViewLink": f"https://drive.google.com/file/d/{fid}/view"}
        return Call(run)

    # sheets
    def spreadsheets(self):
        return self

    def values(self):
        return self

    def batchUpdate(self, spreadsheetId, body):
        return Call(lambda: self.calls.append("freeze"))

    def clear(self, spreadsheetId, range, body):
        return Call(lambda: self.sheet_values.__setitem__(spreadsheetId, []))

    def update(self, body, spreadsheetId=None, range=None, valueInputOption=None, fileId=None, fields=None,
               supportsAllDrives=None):
        if fileId is not None:                       # drive.files().update: 이름 바꾸기
            def rename():
                if fileId not in self.store:
                    raise FakeHttpError(404)
                self.store[fileId]["name"] = body["name"]
                self.calls.append(f"rename:{body['name']}")
            return Call(rename)
        assert valueInputOption == "RAW"
        return Call(lambda: self.sheet_values.__setitem__(spreadsheetId, body["values"]))


@pytest.fixture
def google(service):
    fake = FakeGoogle()
    service.sync = GoogleSync(service, fake, fake, debounce=0)
    service.sync.guild_name = lambda gid: "8조"
    return fake


async def test_ingest_uploads_copy_and_writes_sheet(service, src, google):
    doc = (await service.ingest_file(src, "IR자료.pdf", make_pdf("pitch"), [])).doc
    await service.sync.wait_idle()

    doc = service.db.get_doc(doc["id"])
    assert doc["drive_url"].startswith("https://drive.google.com/file/d/")
    folder = service.db.get_setting(1, "drive_folder_id")
    uploaded = google.store[doc["drive_file_id"]]
    assert uploaded["parents"] == [folder]
    assert uploaded["name"] == f"[{doc['tags'][0]}] IR자료_{doc['created_at'][:10]}.pdf"
    assert doc["drive_name"] == uploaded["name"]
    assert google.store[folder]["name"] == "팀 자료 아카이브 - 8조"

    sheet = service.db.get_setting(1, "drive_sheet_id")
    rows = google.sheet_values[sheet]
    assert rows[0][0] == "id" and "드라이브" in rows[0]
    assert rows[1][2] == "IR자료" and doc["drive_url"] in rows[1]
    links = service.sync.links(1)
    assert links["folder"].endswith(folder) and sheet in links["sheet"] and links["pending"] == 0
    # 로컬 색인에도 드라이브 링크가 들어간다
    index = (service.config.archive_dir / "1" / "INDEX.md").read_text(encoding="utf-8")
    assert doc["drive_url"] in index


async def test_changes_rewrite_sheet_without_reuploading(service, src, google):
    doc = (await service.ingest_file(src, "a.pdf", make_pdf("x"), [])).doc
    await service.sync.wait_idle()
    uploads = sum(c.startswith("create:file") for c in google.calls)
    service.set_intent(doc["id"], "submission", 7, "민수")
    await service.sync.wait_idle()
    sheet = service.db.get_setting(1, "drive_sheet_id")
    assert google.sheet_values[sheet][1][3] == "제출용"
    assert sum(c.startswith("create:file") for c in google.calls) == uploads
    assert google.calls.count("create:application/vnd.google-apps.folder:팀 자료 아카이브 - 8조") == 1


async def test_debounce_batches_quick_changes(service, src, google):
    service.sync.debounce = 0.05
    for i in range(3):
        await service.ingest_file(src, f"f{i}.pdf", make_pdf(f"doc {i}"), [])
    await service.sync.wait_idle()
    sheet = service.db.get_setting(1, "drive_sheet_id")
    assert len(google.sheet_values[sheet]) == 4
    assert google.calls.count("create:application/vnd.google-apps.spreadsheet:팀 자료 목록") == 1


async def test_failed_upload_is_retried_later(service, src, google):
    google.fail_upload = RuntimeError("network down")
    doc = (await service.ingest_file(src, "a.pdf", make_pdf("x"), [])).doc   # 보관 자체는 성공
    await service.sync.wait_idle()
    assert service.db.get_doc(doc["id"])["drive_file_id"] is None
    assert service.db.guilds_without_drive() == [1]

    google.fail_upload = None
    assert await service.sync.sync_guild(1) == 1
    assert service.db.get_doc(doc["id"])["drive_url"]
    assert service.db.guilds_without_drive() == []


async def test_deleted_folder_is_recreated(service, src, google):
    await service.ingest_file(src, "a.pdf", make_pdf("x"), [])
    await service.sync.wait_idle()
    old = service.db.get_setting(1, "drive_folder_id")
    del google.store[old]                     # 누군가 드라이브에서 폴더를 지움
    await service.ingest_file(src, "b.pdf", make_pdf("y"), [])
    await service.sync.wait_idle()            # 실패하면서 설정을 비운다
    assert service.db.get_setting(1, "drive_folder_id") is None
    await service.sync.sync_guild(1)
    assert service.db.get_setting(1, "drive_folder_id") not in (None, old)


async def test_missing_local_copy_is_not_retried(service, src, google):
    doc = (await service.ingest_file(src, "a.pdf", make_pdf("x"), [])).doc
    await service.sync.wait_idle()
    service.db.update_doc(doc["id"], drive_file_id=None, archive_path="gone/a.pdf")
    await service.sync.sync_guild(1)
    assert service.db.get_doc(doc["id"])["drive_file_id"] == ""
    assert service.db.guilds_without_drive() == []


def test_credentials_parsing(tmp_path):
    user = {"refresh_token": "r", "client_id": "c", "client_secret": "s", "token_uri": "https://oauth2.googleapis.com/token"}
    path = tmp_path / "t.json"
    path.write_text(json.dumps(user))
    assert read_credentials_info(str(path)) == read_credentials_info(json.dumps(user)) == user
    creds, is_sa = load_credentials(json.dumps(user))
    assert not is_sa and creds.refresh_token == "r"
    with pytest.raises(ValueError):
        load_credentials(json.dumps({"client_id": "c"}))


def test_service_account_requires_folder(service):
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048).private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()).decode()
    sa = {"type": "service_account", "client_email": "bot@x.iam.gserviceaccount.com", "private_key": key,
          "token_uri": "https://oauth2.googleapis.com/token", "project_id": "p", "private_key_id": "k"}
    creds, is_sa = load_credentials(json.dumps(sa))
    assert is_sa and creds.service_account_email == "bot@x.iam.gserviceaccount.com"
    service.config.google_credentials = json.dumps(sa)
    with pytest.raises(ValueError, match="GOOGLE_DRIVE_FOLDER_ID"):
        GoogleSync.from_config(service, service.config)
    service.config.google_folder_id = "folder123"
    sync = GoogleSync.from_config(service, service.config)   # 실제 구글 클라이언트가 만들어지는지 (호출은 안 함)
    assert sync.root_folder_id == "folder123"


def test_old_database_gets_new_columns(tmp_path):
    path = tmp_path / "old.sqlite3"
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE documents (id INTEGER PRIMARY KEY, guild_id INTEGER, sha256 TEXT, created_at TEXT, "
                 "version_group_id INTEGER, message_id INTEGER)")
    conn.commit()
    conn.close()
    db = Database(path)
    cols = {r["name"] for r in db.conn.execute("PRAGMA table_info(documents)")}
    assert {"drive_file_id", "drive_url"} <= cols


def test_real_google_client_builds_valid_requests(tmp_path):
    """실제 구글 라이브러리로 요청을 만들어 매개변수 이름이 API 정의와 맞는지 확인한다 (네트워크 없이)."""
    from googleapiclient.discovery import build
    from googleapiclient.http import HttpMockSequence

    ok = {"status": "200"}
    http = HttpMockSequence([
        (ok, json.dumps({"id": "folder1"})),
        (ok, json.dumps({"id": "file1", "webViewLink": "https://drive.google.com/file/d/file1/view"})),
        (ok, json.dumps({"id": "sheet1"})),
        (ok, "{}"),   # 머리글 고정
        (ok, json.dumps({"id": "file1"})),   # 이름 바꾸기
        (ok, "{}"),   # clear
        (ok, "{}"),   # update
    ])
    drive = build("drive", "v3", http=http, static_discovery=True)
    sheets = build("sheets", "v4", http=http, static_discovery=True)
    sync = GoogleSync(type("S", (), {"db": None})(), drive, sheets)
    assert sync._create_folder("팀 자료 아카이브", "root1") == "folder1"
    path = tmp_path / "a.pdf"
    path.write_bytes(make_pdf("x"))
    assert sync._upload("folder1", "2026-11-19 a.pdf", path) == ("file1", "https://drive.google.com/file/d/file1/view")
    assert sync._create_sheet("folder1") == "sheet1"
    sync._rename("file1", "[리서치] 새 이름_2026-11-20.pdf")
    sync._write_rows("sheet1", [["id", "제목"], [1, "=1+1"]])


async def test_drive_name_follows_tag_and_title_changes(service, src, google):
    from archivebot.gdrive import drive_file_name

    doc = (await service.ingest_file(src, "scan_0001.pdf", make_pdf("x"), [])).doc
    await service.sync.wait_idle()
    file_id = service.db.get_doc(doc["id"])["drive_file_id"]
    service.set_tags(doc["id"], ["리서치"], 9, "지현")
    service.edit_doc(doc["id"], 9, "지현", title="경쟁사 요금 분석")
    await service.sync.wait_idle()
    date = doc["created_at"][:10]
    assert google.store[file_id]["name"] == f"[리서치] 경쟁사 요금 분석_{date}.pdf"
    assert service.db.get_doc(doc["id"])["drive_name"] == f"[리서치] 경쟁사 요금 분석_{date}.pdf"
    renames = [c for c in google.calls if c.startswith("rename:")]
    await service.sync.sync_guild(1)                       # 바뀐 게 없으면 다시 이름을 바꾸지 않는다
    assert [c for c in google.calls if c.startswith("rename:")] == renames

    # 예전 형식으로 올라간 파일도 새 형식으로 바뀐다
    service.db.update_doc(doc["id"], drive_name="2026-11-19 scan_0001.pdf")
    await service.sync.sync_guild(1)
    assert service.db.get_doc(doc["id"])["drive_name"] == drive_file_name(service.db.get_doc(doc["id"]))

    # 드라이브에서 지워진 파일은 더 손대지 않는다
    del google.store[file_id]
    service.edit_doc(doc["id"], 9, "지현", title="또 바뀐 제목")
    await service.sync.wait_idle()
    assert service.db.get_doc(doc["id"])["drive_file_id"] == ""


async def test_dropped_connection_is_logged_briefly_and_retried_later(service, src, google, caplog):
    import logging
    import ssl

    from archivebot.gdrive import _is_transient

    assert _is_transient(ssl.SSLError("UNEXPECTED_EOF_WHILE_READING"))
    assert _is_transient(FakeHttpError(503)) and not _is_transient(FakeHttpError(404))
    google.fail_upload = ssl.SSLError("[SSL: UNEXPECTED_EOF_WHILE_READING]")
    with caplog.at_level(logging.WARNING, logger="archivebot.gdrive"):
        doc = (await service.ingest_file(src, "a.pdf", make_pdf("x"), [])).doc
        await service.sync.wait_idle()
    assert "잠깐 끊겼습니다" in caplog.text and "Traceback" not in caplog.text
    google.fail_upload = None
    await service.sync.sync_guild(1)
    assert service.db.get_doc(doc["id"])["drive_url"]
