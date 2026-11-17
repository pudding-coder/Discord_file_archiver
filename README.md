# 팀 자료 기억 봇 (Discord File Archiver)

디스코드에 올라온 파일과 링크를 자동으로 보관·분류하고, 대화 중에 `/찾아줘`로 찾아 바로 공유하는 봇입니다.
팀원은 태그 없이 하던 대로 올리면 됩니다.

- **자동 아카이빙**: 파일·링크 사본을 폴더에 저장하고 AI가 제목·요약·주제 태그를 붙입니다.
- **공유 의도 태깅**: 레퍼런스 / 경쟁사 / 피드백 요청 / 제출용. 확신하면 📌만, 애매할 때만 버튼으로 묻습니다.
- **버전 계보**: 비슷한 자료가 올라오면 이전 자료의 v2로 연결하고 바뀐 점을 한 줄로 알려 줍니다.
- **대화 중 검색**: `/찾아줘 질문:본선용 IR 자료` — 채널을 몰라도, 띄어쓰기가 달라도 찾습니다.
- **팀 용어·규칙**: `/용어 등록`, `/규칙 등록`으로 말로 알려 주면 분류와 검색에 반영합니다.
- **AI 연결**: 아카이브 폴더의 `INDEX.md`를 GPT·Claude에 연결해 레퍼런스로 씁니다.

기획 전체는 [docs/기획서.md](docs/기획서.md)에 있습니다.

## 시작하기

### 1. 디스코드 봇 만들기

1. [Discord Developer Portal](https://discord.com/developers/applications)에서 **New Application**을 만듭니다.
2. **Bot** 탭에서 **Reset Token**으로 토큰을 복사하고, **Message Content Intent**를 켭니다. (꺼져 있으면 봇이 파일과 링크를 보지 못합니다.)
3. **OAuth2 → URL Generator**에서 Scopes에 `bot`, `applications.commands`를 고르고, Bot Permissions에 아래 다섯 가지를 고릅니다.
   - View Channels, Send Messages, Read Message History, Add Reactions, Attach Files
4. 만들어진 URL로 팀 서버에 봇을 초대합니다.

### 2. 실행

Python 3.10 이상이 필요합니다.

```bash
pip install -r requirements.txt
cp .env.example .env    # DISCORD_TOKEN, ANTHROPIC_API_KEY, GUILD_IDS를 채웁니다
python -m archivebot
```

- `GUILD_IDS`에 서버 ID를 넣으면 슬래시 명령이 바로 등록됩니다. 비우면 전역 등록이라 반영까지 최대 1시간이 걸립니다.
  (서버 ID: 디스코드 설정 → 고급 → 개발자 모드를 켜고 서버 아이콘 우클릭 → ID 복사)
- `ANTHROPIC_API_KEY`가 없어도 실행됩니다. 이때는 키워드 기반 간이 분류로 동작하고 모든 자료에 확인 버튼이 뜹니다.

Docker로 실행할 수도 있습니다.

```bash
docker build -t archivebot .
docker run -d --env-file .env -v "$(pwd)/data:/app/data" archivebot
```

### 3. 써 보기

1. 채널에 PDF를 올립니다. 📌 반응이 붙으면 보관된 것입니다.
2. 다른 채널에서 `/찾아줘`로 찾고 [채널에 공유]를 누릅니다.
3. 메시지 우클릭 → 앱 → **자료 정보**에서 요약과 버전 계보를 보고 공유 의도를 고칩니다.

## 명령어

| 명령 | 하는 일 |
|---|---|
| `/찾아줘 질문` | 기억나는 말로 자료 검색. 결과는 나에게만 먼저 보이고 버튼으로 채널에 공유 |
| `/최근 [개수]` | 최근 보관 자료 목록 |
| `/수정 자료번호 [의도] [제목] [태그]` | AI가 붙인 값 수정 (모두 기록됨) |
| `자료 정보` (메시지 우클릭 → 앱) | 자료 요약·버전 계보 보기, 의도 수정 |
| `/용어 등록·목록·삭제` | 팀 용어 사전. 예: `경쟁사조사=벤치마킹` |
| `/규칙 등록·목록·삭제` | 팀 정리 규칙. 예: `리서치 자료는 제목 앞에 [리서치]` |
| `/수집 상태` | 이 채널의 자동 보관 켜기/끄기 (채널 관리 권한) |
| `/과거수집 [메시지수]` | 지난 메시지에서 자료를 찾아 보관 (서버 관리 권한) |
| `/카톡가져오기 파일` | 카카오톡 대화 내보내기(.txt)에서 링크 가져오기 |
| `/통계 [기간]` | 재요청 수, AI 태그 수정률, 검색 소요 시간, 재사용률 |
| `/내보내기` | 자료 목록 CSV |

## 설정

`.env`에서 바꿉니다. 전체 항목과 설명은 [.env.example](.env.example)에 있습니다.

| 항목 | 기본값 | 설명 |
|---|---|---|
| `ARCHIVE_DIR` | `./data/archive` | 사본과 색인이 쌓이는 폴더. 구글 드라이브 동기화 폴더로 지정하면 공유 드라이브에 보관됩니다. |
| `ARCHIVE_BASE_URL` | 없음 | 위 폴더의 드라이브 웹 주소. 지정하면 검색 결과에 폴더 링크가 붙습니다. |
| `TOPIC_TAGS` | 기획, 리서치, … | 주제 태그 고정 목록 |
| `CONFIDENCE_THRESHOLD` | `0.75` | AI 확신도가 이보다 낮으면 버튼으로 확인을 요청합니다. |
| `CONTEXT_MESSAGES` | `3` | 분류할 때 함께 읽는 직전 메시지 수. `0`이면 읽지 않습니다. |
| `ANTHROPIC_MODEL` | `claude-opus-5-5` | 분류·검색에 쓰는 모델 |

## GPT·Claude에 연결하기

`ARCHIVE_DIR/<서버 ID>/` 폴더에 자료 사본과 함께 `INDEX.md`(제목·요약·공유 의도·버전·파일 위치), `index.csv`가 유지됩니다.
이 폴더를 구글 드라이브로 동기화한 뒤 ChatGPT나 Claude의 드라이브 연결에 추가하면, AI가 색인을 읽고 필요한 자료를 찾아 씁니다.

## 개인정보

- 봇은 파일이나 링크가 있는 메시지만 처리합니다. 다른 메시지는 저장하지 않습니다.
- 자료 본문과 직전 대화 몇 줄이 분류를 위해 Claude API로 전송됩니다. 설치 전에 팀원 동의를 받으세요.
- 민감한 채널은 `/수집 상태:끄기`로 제외합니다.
- 봇은 원본 메시지를 옮기거나 지우지 않습니다. 메시지 관리 권한을 주지 마세요.

## 개발

```bash
pip install -r requirements-dev.txt
python -m pytest
```

| 파일 | 역할 |
|---|---|
| `archivebot/bot.py` | 디스코드 연결: 메시지 수집, 확인 버튼, 슬래시 명령 |
| `archivebot/service.py` | 핵심 로직: 보관, 버전 연결, 검색, 수정 기록, 색인 |
| `archivebot/llm.py` | Claude 분류·검색 재정렬과 키워드 기반 간이 분류 |
| `archivebot/search.py` | 글자 2-gram 어휘 검색, 버전 후보 탐지 |
| `archivebot/extract.py` | PDF·텍스트·링크 본문 추출 |
| `archivebot/kakao.py` | 카카오톡 대화 내보내기 파싱 |
| `archivebot/db.py` | SQLite 스키마와 조회 |

## 아직 없는 것

- 스캔 PDF·이미지 OCR, PPT·DOCX·HWP 본문 추출 (사본은 보관하고 파일명과 대화 맥락으로 분류합니다)
- 구글 드라이브·시트 API 직접 연동 (동기화 폴더로 대신합니다)
- 카카오 챗봇 1:1 전달 (대화 내보내기 가져오기만 지원합니다)
