# 팀 자료 기억 봇 (Discord File Archiver)

디스코드에 올라온 파일과 링크를 자동으로 보관·분류하고, 대화 중에 `/찾아줘`로 찾아 바로 공유하는 봇입니다.
팀원은 태그 없이 하던 대로 올리면 됩니다.

- **자동 아카이빙**: 파일·링크 사본을 폴더에 저장하고 AI가 제목·요약·주제 태그를 붙입니다.
- **거의 모든 문서 읽기**: PDF, PPTX, DOCX, XLSX, HWP, HWPX의 본문을 읽고, 스캔 PDF와 이미지는 AI가 직접 읽습니다(OCR). 밈·짤 이미지는 보관하지 않습니다.
- **구글 드라이브·시트**: 자료 사본을 드라이브 폴더에 올리고, 전체 목록을 스프레드시트로 유지합니다.
- **공유 의도 태깅**: 레퍼런스 / 경쟁사 / 피드백 요청 / 제출용. 확신하면 📌만, 애매할 때만 버튼으로 묻습니다.
- **버전 계보**: 비슷한 자료가 올라오면 이전 자료의 v2로 연결하고 바뀐 점을 한 줄로 알려 줍니다.
- **대화 중 검색**: `/찾아줘 질문:본선용 IR 자료` — 채널을 몰라도, 띄어쓰기가 달라도 찾습니다.
- **팀 용어·규칙**: `/용어 등록`, `/규칙 등록`으로 말로 알려 주면 분류와 검색에 반영합니다.
- **AI 연결**: 드라이브 폴더를 ChatGPT·Gemini·Claude에 연결하면 AI가 팀 자료와 목록을 바로 읽습니다.

기획 전체는 [docs/기획서.md](docs/기획서.md)에 있습니다.

## 시작하기

### 1. 디스코드 봇 만들기

1. [Discord Developer Portal](https://discord.com/developers/applications)에서 **New Application**을 만듭니다.
2. **Bot** 탭에서 **Reset Token**으로 토큰을 복사하고, **Message Content Intent**를 켭니다. (꺼져 있으면 봇이 파일과 링크를 보지 못합니다.)
3. **OAuth2 → URL Generator**에서 Scopes에 `bot`, `applications.commands`를 고르고, Bot Permissions에 아래 다섯 가지를 고릅니다.
   - View Channels, Send Messages, Read Message History, Add Reactions, Attach Files
4. 만들어진 URL로 팀 서버에 봇을 초대합니다.

   URL Generator를 못 찾겠으면 **General Information**의 **Application ID**를 복사해 아래 주소의 `<APPLICATION_ID>`를 바꿔 쓰면 됩니다. 위 다섯 가지 권한이 이미 들어 있습니다.

   ```
   https://discord.com/oauth2/authorize?client_id=<APPLICATION_ID>&scope=bot+applications.commands&permissions=101440
   ```

### 2. 실행

Python 3.10 이상이 필요합니다.

```bash
pip install -r requirements.txt
cp .env.example .env    # DISCORD_TOKEN, GEMINI_API_KEY, GUILD_IDS를 채웁니다
python -m archivebot
```

- `GUILD_IDS`에 서버 ID를 넣으면 슬래시 명령이 바로 등록됩니다. 비우면 전역 등록이라 반영까지 최대 1시간이 걸립니다.
  (서버 ID: 디스코드 설정 → 고급 → 개발자 모드를 켜고 서버 아이콘 우클릭 → ID 복사)
- `GEMINI_API_KEY`는 [Google AI Studio](https://aistudio.google.com/apikey)에서 무료로 발급받습니다.
- 키가 없어도 실행됩니다. 이때는 키워드 기반 간이 분류로 동작하고, 모든 자료에 확인 버튼이 뜨며, 이미지·스캔 PDF는 읽지 못합니다.

### Gemini 무료 등급에서 알아 둘 것

- **하루·분당 요청 수가 적습니다.** 자료 1건 분류와 `/찾아줘` 1번이 각각 요청 1회입니다. 한도를 넘으면 봇은 멈추지 않고 키워드 기반 분류·글자 검색으로 대신합니다(이미지는 그동안 보관하지 않습니다). 정확한 한도는 [AI Studio](https://aistudio.google.com)의 사용량 화면에서 확인하세요.
- **무료 등급에 보낸 내용은 Google이 서비스 개선에 쓸 수 있습니다.** Google 약관상 무료 등급 요청은 사람이 검토하거나 모델 개선에 쓰일 수 있으니, 민감한 자료가 올라오는 채널은 `/수집 끄기`로 빼거나 결제 등급을 쓰세요.
- 모델은 `GEMINI_MODEL`로 바꿉니다. 기본값 `gemini-flash-latest`는 최신 Flash 모델을 가리킵니다.
- Claude를 쓰고 싶으면 `ANTHROPIC_API_KEY`를 넣고 `AI_PROVIDER=claude`로 바꾸면 됩니다.

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
| `/드라이브` | 자료가 쌓이는 구글 드라이브 폴더와 목록 시트 링크 |
| `/통계 [기간]` | 재요청 수, AI 태그 수정률, 검색 소요 시간, 재사용률 |
| `/내보내기` | 자료 목록 CSV |

## 설정

`.env`에서 바꿉니다. 전체 항목과 설명은 [.env.example](.env.example)에 있습니다.

| 항목 | 기본값 | 설명 |
|---|---|---|
| `ARCHIVE_DIR` | `./data/archive` | 사본과 색인이 쌓이는 폴더 |
| `GOOGLE_CREDENTIALS` | 없음 | 구글 드라이브 연동 토큰 또는 서비스 계정 키 (아래 "구글 드라이브 연결하기") |
| `GOOGLE_DRIVE_FOLDER_ID` | 없음 | 서비스 계정이 자료를 올릴 공유 드라이브 폴더 ID |
| `OCR_MAX_PAGES` | `20` | 스캔 PDF에서 AI가 읽는 최대 쪽수 |
| `ARCHIVE_BASE_URL` | 없음 | 위 폴더의 드라이브 웹 주소. 지정하면 검색 결과에 폴더 링크가 붙습니다. |
| `TOPIC_TAGS` | 기획, 리서치, … | 주제 태그 고정 목록 |
| `CONFIDENCE_THRESHOLD` | `0.75` | AI 확신도가 이보다 낮으면 버튼으로 확인을 요청합니다. |
| `CONTEXT_MESSAGES` | `3` | 분류할 때 함께 읽는 직전 메시지 수. `0`이면 읽지 않습니다. |
| `GEMINI_MODEL` | `gemini-flash-latest` | 분류·검색에 쓰는 Gemini 모델 |
| `AI_PROVIDER` | 자동 | `gemini` 또는 `claude`. 비우면 키가 있는 쪽 (둘 다 있으면 Gemini) |

## 읽을 수 있는 파일

| 형식 | 읽는 방법 |
|---|---|
| PDF (글자 있음), TXT, MD, CSV | 봇이 직접 읽음 |
| DOCX, PPTX(발표자 노트·표 포함), XLSX, HWP, HWPX | 봇이 직접 읽음 |
| 스캔 PDF | 글자가 거의 없으면 앞 20쪽(`OCR_MAX_PAGES`)을 AI가 읽음 |
| 이미지 (PNG, JPG, WEBP) | AI가 읽고, 업무 자료일 때만 보관 (밈·잡담 사진은 무시) |
| DOC, PPT, XLS(옛 형식), 암호·배포용 HWP, KEY, ZIP | 사본만 보관하고 파일명과 대화 맥락으로 분류 |

스캔 PDF와 이미지는 AI 키(`GEMINI_API_KEY`)가 있어야 읽습니다. 크기가 큰 요청이라 무료 한도를 빨리 씁니다.

## 구글 드라이브 연결하기

연결하면 자료가 올라올 때마다 아래 일이 자동으로 일어납니다.

- 드라이브에 「팀 자료 아카이브 - 서버 이름」 폴더를 만들고 자료 사본을 올립니다.
- 같은 폴더의 「팀 자료 목록」 시트에 전체 목록(제목·의도·버전·요약·링크)을 씁니다.
- `/찾아줘` 결과에 **드라이브에서 열기** 링크가 붙습니다.
- `/드라이브`로 폴더와 시트 링크를 볼 수 있습니다.

시트는 봇이 계속 덮어쓰므로 직접 고치지 말고 `/수정`으로 고치세요.
업로드에 실패하면 10분마다 다시 시도하니, 구글이 잠깐 안 돼도 자료는 사라지지 않습니다.

### 개인 구글 계정으로 연결 (대부분 이쪽)

봇은 **자기가 만든 파일만** 볼 수 있는 권한(`drive.file`)만 받습니다. 내 드라이브의 다른 파일은 볼 수 없습니다.

1. [Google Cloud Console](https://console.cloud.google.com)에서 새 프로젝트를 만듭니다.
2. **API 및 서비스 → 라이브러리**에서 **Google Drive API**와 **Google Sheets API**를 각각 **사용**으로 켭니다.
3. **OAuth 동의 화면**(Google Auth Platform)을 설정합니다.
   - 사용자 유형은 **외부**, 앱 이름과 내 이메일을 넣습니다.
   - **대상**에서 게시 상태를 **프로덕션**으로 바꿉니다. **테스트**로 두면 토큰이 7일 뒤 만료되어 연동이 끊깁니다.
4. **사용자 인증 정보(클라이언트) → OAuth 클라이언트 ID 만들기**에서 유형을 **데스크톱 앱**으로 고르고, JSON을 내려받아 프로젝트 폴더에 `client_secret.json`으로 저장합니다.
5. 내 컴퓨터에서 한 번 실행합니다.
   ```bash
   python -m archivebot.google_auth client_secret.json
   ```
   브라우저가 열리면 로그인하고 허용합니다. "확인되지 않은 앱" 경고가 뜨면 **고급 → (앱 이름)(으)로 이동**을 누르세요. 내가 만든 앱이라 괜찮습니다.
6. 만들어진 `google_token.json`을 `.env`에 넣습니다.
   ```
   GOOGLE_CREDENTIALS=google_token.json
   ```
   Railway에서는 파일 내용 전체를 `GOOGLE_CREDENTIALS` 변수 값으로 붙여 넣습니다.
7. 봇을 다시 실행하고 디스코드에서 `/드라이브`를 입력해 폴더 링크를 엽니다. 드라이브에서 그 폴더를 **팀원과 공유**합니다.

`client_secret.json`과 `google_token.json`은 비밀번호와 같습니다. `.gitignore`에 넣어 두었으니 깃허브에 올리지 마세요.

### 공유 드라이브로 연결 (Google Workspace 학교·회사 계정)

공유 드라이브를 쓸 수 있다면 서비스 계정이 더 깔끔합니다. 토큰 만료가 없고, 팀원이 이미 공유 드라이브 멤버라 따로 공유할 필요가 없습니다.

1. 위 1~2단계와 같이 프로젝트를 만들고 API를 켭니다.
2. **IAM 및 관리자 → 서비스 계정**에서 서비스 계정을 만들고 **키 → JSON** 키를 내려받습니다.
3. 공유 드라이브에 폴더를 만들고, 서비스 계정 이메일(`...@...iam.gserviceaccount.com`)을 **콘텐츠 관리자**로 추가합니다.
4. `.env`에 키 파일과 폴더 ID(폴더 주소의 `folders/` 뒤 부분)를 넣습니다.
   ```
   GOOGLE_CREDENTIALS=service-account.json
   GOOGLE_DRIVE_FOLDER_ID=1AbC...
   ```

서비스 계정은 저장 용량이 없어서 개인 "내 드라이브" 폴더에는 파일을 올리지 못합니다. 개인 계정이면 위의 OAuth 방법을 쓰세요.

## ChatGPT·Gemini·Claude에 연결하기

드라이브를 연결했다면 「팀 자료 아카이브」 폴더를 ChatGPT·Gemini·Claude의 구글 드라이브 연결에 추가하면 됩니다. AI가 「팀 자료 목록」 시트로 자료를 찾고 원본을 읽습니다.

드라이브 없이 쓸 때는 `ARCHIVE_DIR/<서버 ID>/` 폴더에 자료 사본과 `INDEX.md`, `index.csv`가 쌓입니다.

## 개인정보

- 봇은 파일이나 링크가 있는 메시지만 처리합니다. 다른 메시지는 저장하지 않습니다.
- 자료 본문(스캔 PDF·이미지는 원본)과 직전 대화 몇 줄이 분류를 위해 Gemini API(또는 설정한 AI)로 전송됩니다. 설치 전에 팀원 동의를 받으세요.
- Gemini 무료 등급에 보낸 내용은 Google이 서비스 개선에 쓸 수 있습니다. 민감한 채널은 수집을 끄세요.
- 이미지는 AI가 읽어 본 뒤 업무 자료가 아니면 저장하지 않습니다.
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
| `archivebot/llm.py` | Gemini·Claude 분류·검색 재정렬과 키워드 기반 간이 분류 |
| `archivebot/search.py` | 글자 2-gram 어휘 검색, 버전 후보 탐지 |
| `archivebot/extract.py` | PDF·DOCX·PPTX·XLSX·HWP·HWPX·링크 본문 추출, OCR 준비 |
| `archivebot/gdrive.py` | 구글 드라이브 업로드·목록 시트 동기화 |
| `archivebot/google_auth.py` | 개인 구글 계정 토큰 만들기 |
| `archivebot/kakao.py` | 카카오톡 대화 내보내기 파싱 |
| `archivebot/db.py` | SQLite 스키마와 조회 |

## 아직 없는 것

- 옛 형식(DOC·PPT·XLS) 본문 추출, PPTX 안 이미지의 OCR
- 시트에서 직접 고친 내용을 봇으로 가져오기 (시트는 읽기 전용 목록입니다)
- 카카오 챗봇 1:1 전달 (대화 내보내기 가져오기만 지원합니다)
