"""개인 구글 계정으로 드라이브 연동 토큰을 만든다. 자기 컴퓨터에서 한 번만 실행한다.

    python -m archivebot.google_auth client_secret.json

브라우저에서 구글 로그인 → 허용을 누르면 google_token.json이 만들어진다.
그 파일 경로(또는 내용 전체)를 .env의 GOOGLE_CREDENTIALS에 넣는다.
"""
from __future__ import annotations

import sys
from pathlib import Path

from .gdrive import SCOPES_USER


def main() -> None:
    if len(sys.argv) < 2:
        sys.exit("사용법: python -m archivebot.google_auth <구글 클라우드에서 받은 client_secret.json>")
    from google_auth_oauthlib.flow import InstalledAppFlow

    flow = InstalledAppFlow.from_client_secrets_file(sys.argv[1], scopes=SCOPES_USER)
    creds = flow.run_local_server(port=0, prompt="consent", access_type="offline")
    out = Path(sys.argv[2] if len(sys.argv) > 2 else "google_token.json")
    out.write_text(creds.to_json(), encoding="utf-8")
    try:
        out.chmod(0o600)
    except OSError:
        pass
    print(f"\n저장했어요: {out.resolve()}")
    print("로컬 실행: .env에 GOOGLE_CREDENTIALS=" + str(out.resolve()))
    print("Railway 등 클라우드: 이 파일 내용 전체를 GOOGLE_CREDENTIALS 변수 값으로 붙여 넣으세요.")
    print("이 파일은 비밀번호와 같아요. 깃허브에 올리거나 채팅에 붙여 넣지 마세요.")


if __name__ == "__main__":
    main()
