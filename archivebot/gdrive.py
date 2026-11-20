"""구글 드라이브·시트 연동.

- 보관한 자료의 사본을 서버별 드라이브 폴더에 올리고, 자료마다 드라이브 링크를 남긴다.
- 같은 폴더의 「팀 자료 목록」 스프레드시트에 전체 목록을 쓴다. 의도·버전이 바뀌면 다시 쓴다.
- 이 폴더를 ChatGPT·Claude의 드라이브 연결에 추가하면 AI가 목록과 원본을 바로 읽는다.

인증은 두 가지 중 하나를 GOOGLE_CREDENTIALS에 넣는다 (JSON 문자열 또는 파일 경로).
- 개인 구글 계정: `python -m archivebot.google_auth`로 만든 OAuth 토큰. 봇은 자기가 만든 파일만
  볼 수 있는 권한(drive.file)만 받고, 내 드라이브에 「팀 자료 아카이브」 폴더를 직접 만든다.
- 서비스 계정 키: 공유 드라이브(Google Workspace)의 폴더를 서비스 계정과 공유하고 그 폴더 ID를
  GOOGLE_DRIVE_FOLDER_ID에 넣는다. 서비스 계정은 저장 용량이 없어 일반 내 드라이브에는 올리지 못한다.

구글 API 호출은 모두 별도 스레드에서 하고(블로킹 라이브러리), DB는 이벤트 루프에서만 만진다.
업로드에 실패한 자료는 drive_file_id가 비어 있으므로 주기적으로 다시 시도한다.
"""
from __future__ import annotations

import asyncio
import json
import logging
import mimetypes
import re
from pathlib import Path
from typing import Callable

log = logging.getLogger(__name__)

FOLDER_MIME = "application/vnd.google-apps.folder"
SHEET_MIME = "application/vnd.google-apps.spreadsheet"
SCOPES_USER = ["https://www.googleapis.com/auth/drive.file"]
SCOPES_SERVICE_ACCOUNT = ["https://www.googleapis.com/auth/drive"]
RESUMABLE_OVER = 5 * 1024 * 1024
# 연결 끊김(SSL EOF 등)·5xx는 새 연결로 다시 시도한다. 오래 쉬던 연결을 구글이 닫아 두는 경우가 흔하다.
RETRIES = 3
SHEET_TITLE = "팀 자료 목록"


def read_credentials_info(raw: str) -> dict:
    raw = raw.strip()
    text = raw if raw.startswith("{") else Path(raw).expanduser().read_text(encoding="utf-8")
    return json.loads(text)


def load_credentials(raw: str):
    """(credentials, 서비스 계정 여부)."""
    info = read_credentials_info(raw)
    if info.get("type") == "service_account":
        from google.oauth2 import service_account

        return service_account.Credentials.from_service_account_info(info, scopes=SCOPES_SERVICE_ACCOUNT), True
    from google.oauth2.credentials import Credentials

    if not info.get("refresh_token"):
        raise ValueError("GOOGLE_CREDENTIALS에 refresh_token이 없어요. `python -m archivebot.google_auth`로 다시 만들어 주세요.")
    return Credentials.from_authorized_user_info(info, scopes=SCOPES_USER), False


def folder_url(folder_id: str) -> str:
    return f"https://drive.google.com/drive/folders/{folder_id}"


def sheet_url(sheet_id: str) -> str:
    return f"https://docs.google.com/spreadsheets/d/{sheet_id}/edit"


def drive_file_name(doc: dict) -> str:
    """드라이브 파일 이름: AI 제목 (v2)_날짜_[첫 태그].확장자  예) 경쟁사 요금 분석_2026-11-20_[리서치].pdf"""
    tag = (doc.get("tags") or [""])[0].strip()
    title = (doc.get("title") or doc.get("filename") or "자료").strip()
    if tag and title.startswith(f"[{tag}]"):
        title = title[len(tag) + 2:].strip()      # 팀 규칙으로 제목에 이미 태그가 붙어 있으면 한 번만
    version_no = doc.get("version_no") or 1
    version = f" v{version_no}" if version_no > 1 and f"v{version_no}" not in title.lower() else ""
    parts = [f"{title}{version}", (doc.get("created_at") or "")[:10], f"[{tag}]" if tag else ""]
    name = "_".join(p for p in parts if p)
    name = re.sub(r"[\\/\x00-\x1f]", "·", name)[:150]
    filename = doc.get("filename") or ""
    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ("md" if doc.get("kind") == "link" else "")
    return f"{name}.{ext}" if ext else name


def _is_transient(error: Exception) -> bool:
    """네트워크가 잠깐 끊긴 오류인가 (다음 동기화 때 저절로 풀린다)."""
    import socket
    import ssl

    status = _status(error)
    return isinstance(error, (ssl.SSLError, ConnectionError, TimeoutError, socket.timeout)) or \
        (status is not None and status >= 500)


def _status(error: Exception) -> int | None:
    resp = getattr(error, "resp", None)
    status = getattr(resp, "status", None) or getattr(error, "status_code", None) or getattr(error, "status", None)
    try:
        return int(status) if status is not None else None
    except (TypeError, ValueError):
        return None


class GoogleSync:
    def __init__(self, service, drive, sheets, root_folder_id: str = "", debounce: float = 5.0):
        self.service = service
        self.db = service.db
        self.drive = drive
        self.sheets = sheets
        self.root_folder_id = root_folder_id or None
        self.debounce = debounce
        self.guild_name: Callable[[int], str] = str
        self._lock = asyncio.Lock()
        self._timers: dict[int, asyncio.Task] = {}
        self._running: set[asyncio.Task] = set()
        self._dirty: set[int] = set()

    @classmethod
    def from_config(cls, service, config) -> "GoogleSync":
        creds, is_service_account = load_credentials(config.google_credentials)
        if is_service_account and not config.google_folder_id:
            raise ValueError("서비스 계정을 쓰려면 공유 드라이브 폴더 ID를 GOOGLE_DRIVE_FOLDER_ID에 넣어야 해요.")
        from googleapiclient.discovery import build

        drive = build("drive", "v3", credentials=creds, cache_discovery=False)
        sheets = build("sheets", "v4", credentials=creds, cache_discovery=False)
        return cls(service, drive, sheets, root_folder_id=config.google_folder_id)

    # ── 예약·반복 ─────────────────────────────────────────
    def schedule(self, guild_id: int, delay: float | None = None) -> None:
        """변경이 생기면 잠시 모았다가 한 번에 반영한다 (연달아 올린 파일마다 시트를 다시 쓰지 않게)."""
        self._dirty.add(guild_id)
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return   # 이벤트 루프 밖(스크립트·테스트): 다음 주기 동기화 때 반영
        old = self._timers.get(guild_id)
        if old is not None and not old.done():
            old.cancel()
        self._timers[guild_id] = loop.create_task(self._later(guild_id, self.debounce if delay is None else delay))

    async def _later(self, guild_id: int, delay: float) -> None:
        await asyncio.sleep(delay)
        # 대기 중에만 취소되도록, 실제 동기화는 따로 띄운다 (업로드 도중 취소되면 중복 업로드가 생긴다)
        task = asyncio.ensure_future(self.sync_guild(guild_id))
        self._running.add(task)
        task.add_done_callback(self._running.discard)
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            if _is_transient(e):
                log.warning("드라이브 연결이 잠깐 끊겼습니다 (서버 %s): %s — 나중에 다시 시도합니다", guild_id, e)
            else:
                log.exception("드라이브 동기화 실패 (서버 %s) — 나중에 다시 시도합니다", guild_id)

    async def run_forever(self, interval: float = 600) -> None:
        """올리지 못한 자료와 반영하지 못한 변경을 주기적으로 다시 시도한다.
        봇이 켜질 때는 모든 서버를 한 번 맞춰 둔다 (이름 규칙이 바뀌었으면 예전 파일 이름도 고친다)."""
        first = True
        while True:
            targets = set(self.db.guilds_without_drive()) | self._dirty
            if first:
                targets |= set(self.db.guild_ids())
                first = False
            for guild_id in sorted(targets):
                try:
                    await self.sync_guild(guild_id)
                except Exception:
                    log.exception("드라이브 동기화 재시도 실패 (서버 %s)", guild_id)
            await asyncio.sleep(interval)

    async def wait_idle(self) -> None:
        """예약된 동기화가 모두 끝날 때까지 기다린다 (테스트·종료용)."""
        while any(not t.done() for t in self._timers.values()) or self._running:
            await asyncio.gather(*self._timers.values(), *self._running, return_exceptions=True)

    # ── 동기화 ────────────────────────────────────────────
    async def sync_guild(self, guild_id: int) -> int:
        """올리지 않은 자료를 올리고 시트를 다시 쓴다. 새로 올린 자료 수를 돌려준다."""
        async with self._lock:
            self._dirty.discard(guild_id)
            failures: list[Exception] = []
            try:
                folder = await self._folder(guild_id)
                # 단계마다 따로 돈다: 파일 하나가 계속 실패해도 이름 정리·시트 갱신은 된다
                uploaded = await self._upload_pending(guild_id, folder, failures)
                renamed = await self._rename_changed(guild_id, failures)
                if uploaded or renamed:
                    self.service.refresh_index(guild_id, schedule_sync=False)   # 로컬 색인에도 드라이브 링크
                try:
                    await self._write_sheet(guild_id, folder)
                except Exception as e:
                    failures.append(e)
            except Exception:
                self._dirty.add(guild_id)
                raise
            if uploaded or renamed or failures:
                log.info("드라이브 동기화 (서버 %s): 업로드 %d · 이름 변경 %d · 실패 %d",
                         guild_id, uploaded, renamed, len(failures))
            if failures:
                self._dirty.add(guild_id)   # 다음 주기에 다시 시도
                raise failures[0]
            return uploaded

    async def _upload_pending(self, guild_id: int, folder: str, failures: list[Exception] | None = None) -> int:
        uploaded = 0
        for doc in self.db.docs_without_drive(guild_id):
            path = self.service.archive_file(doc)
            if path is None:
                self.db.update_doc(doc["id"], drive_file_id="")   # 사본이 사라진 자료는 다시 시도하지 않는다
                continue
            name = drive_file_name(doc)
            try:
                file_id, url = await asyncio.to_thread(self._upload, folder, name, path)
            except Exception as e:
                if _status(e) == 404:
                    # 누군가 폴더를 지웠다: 다음 동기화 때 폴더와 시트를 새로 만든다
                    self.db.set_setting(guild_id, "drive_folder_id", None)
                    self.db.set_setting(guild_id, "drive_sheet_id", None)
                    raise
                if failures is None:
                    raise
                log.warning("드라이브 업로드 실패 (자료 #%s %s): %s", doc["id"], name, e)
                failures.append(e)
                continue
            self.db.update_doc(doc["id"], drive_file_id=file_id, drive_url=url, drive_name=name)
            uploaded += 1
        return uploaded

    async def _rename_changed(self, guild_id: int, failures: list[Exception] | None = None) -> int:
        """태그·제목·버전이 바뀐 자료는 드라이브 파일 이름도 맞춘다 (예전 형식으로 올라간 파일 포함)."""
        renamed = 0
        for doc in self.db.docs_on_drive(guild_id):
            name = drive_file_name(doc)
            if name == doc.get("drive_name"):
                continue
            try:
                await asyncio.to_thread(self._rename, doc["drive_file_id"], name)
            except Exception as e:
                if _status(e) == 404:
                    # 드라이브에서 누군가 지운 파일: 다시 올리지도, 이름을 고치지도 않는다
                    self.db.update_doc(doc["id"], drive_file_id="", drive_name=None)
                    continue
                if failures is None:
                    raise
                log.warning("드라이브 이름 변경 실패 (자료 #%s → %s): %s", doc["id"], name, e)
                failures.append(e)
                continue
            self.db.update_doc(doc["id"], drive_name=name)
            renamed += 1
        return renamed

    async def _write_sheet(self, guild_id: int, folder: str) -> None:
        sheet = self.db.get_setting(guild_id, "drive_sheet_id")
        if not sheet:
            sheet = await asyncio.to_thread(self._create_sheet, folder)
            self.db.set_setting(guild_id, "drive_sheet_id", sheet)
        rows = self.service.export_rows(guild_id)
        try:
            await asyncio.to_thread(self._write_rows, sheet, rows)
        except Exception as e:
            if _status(e) == 404:
                self.db.set_setting(guild_id, "drive_sheet_id", None)
            raise

    async def _folder(self, guild_id: int) -> str:
        folder = self.db.get_setting(guild_id, "drive_folder_id")
        if not folder:
            name = f"팀 자료 아카이브 - {self.guild_name(guild_id)}"
            folder = await asyncio.to_thread(self._create_folder, name, self.root_folder_id)
            self.db.set_setting(guild_id, "drive_folder_id", folder)
        return folder

    def links(self, guild_id: int) -> dict:
        folder = self.db.get_setting(guild_id, "drive_folder_id")
        sheet = self.db.get_setting(guild_id, "drive_sheet_id")
        return {
            "folder": folder_url(folder) if folder else None,
            "sheet": sheet_url(sheet) if sheet else None,
            "pending": len(self.db.docs_without_drive(guild_id)),
        }

    # ── 구글 API (별도 스레드에서 실행) ───────────────────
    def _create_folder(self, name: str, parent: str | None) -> str:
        body: dict = {"name": name, "mimeType": FOLDER_MIME}
        if parent:
            body["parents"] = [parent]
        return self.drive.files().create(body=body, fields="id", supportsAllDrives=True).execute(num_retries=RETRIES)["id"]

    def _upload(self, folder: str, name: str, path: Path) -> tuple[str, str]:
        from googleapiclient.http import MediaFileUpload

        mime = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        media = MediaFileUpload(str(path), mimetype=mime, resumable=path.stat().st_size > RESUMABLE_OVER)
        created = self.drive.files().create(
            body={"name": name, "parents": [folder]}, media_body=media,
            fields="id,webViewLink", supportsAllDrives=True,
        ).execute(num_retries=RETRIES)
        file_id = created["id"]
        return file_id, created.get("webViewLink") or f"https://drive.google.com/file/d/{file_id}/view"

    def _rename(self, file_id: str, name: str) -> None:
        self.drive.files().update(fileId=file_id, body={"name": name}, fields="id",
                                  supportsAllDrives=True).execute(num_retries=RETRIES)

    def _create_sheet(self, folder: str) -> str:
        created = self.drive.files().create(
            body={"name": SHEET_TITLE, "mimeType": SHEET_MIME, "parents": [folder]},
            fields="id", supportsAllDrives=True,
        ).execute(num_retries=RETRIES)
        sheet_id = created["id"]
        try:   # 머리글 고정 (실패해도 목록은 쓸 수 있다)
            self.sheets.spreadsheets().batchUpdate(spreadsheetId=sheet_id, body={"requests": [{
                "updateSheetProperties": {
                    "properties": {"sheetId": 0, "gridProperties": {"frozenRowCount": 1}},
                    "fields": "gridProperties.frozenRowCount",
                }}]}).execute(num_retries=RETRIES)
        except Exception:
            log.warning("시트 머리글을 고정하지 못했습니다", exc_info=True)
        return sheet_id

    def _write_rows(self, sheet_id: str, rows: list[list]) -> None:
        values = self.sheets.spreadsheets().values()
        values.clear(spreadsheetId=sheet_id, range="A:Z", body={}).execute(num_retries=RETRIES)
        # RAW: 제목이 "="로 시작해도 수식으로 실행되지 않게
        values.update(spreadsheetId=sheet_id, range="A1", valueInputOption="RAW", body={"values": rows}).execute(num_retries=RETRIES)
