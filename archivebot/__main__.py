from __future__ import annotations

import logging
import sys

from dotenv import load_dotenv

from .config import Config
from .db import Database
from .service import ArchiveService


def main() -> None:
    load_dotenv()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    # 외부 라이브러리가 요청마다 남기는 안내 문구는 숨긴다 (경고·오류는 그대로 보인다)
    for noisy in ("google_genai", "httpx", "googleapiclient.discovery_cache"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    log = logging.getLogger("archivebot")
    config = Config.from_env()
    if not config.discord_token:
        sys.exit("DISCORD_TOKEN이 없습니다. .env.example을 .env로 복사해 토큰을 넣어 주세요.")

    from .llm import make_analyzer

    analyzer = make_analyzer(config)
    if analyzer is not None:
        log.info("AI 분류: %s (%s)", config.ai_provider, analyzer.model)
    else:
        log.warning("GEMINI_API_KEY가 없어 키워드 기반 간이 분류로 동작합니다 "
                    "(모든 자료에 확인 버튼이 뜨고, 이미지·스캔 PDF는 읽지 못합니다).")

    config.archive_dir.mkdir(parents=True, exist_ok=True)
    service = ArchiveService(config, Database(config.db_path), analyzer)

    if config.google_credentials:
        from .gdrive import GoogleSync

        try:
            service.sync = GoogleSync.from_config(service, config)
            log.info("구글 드라이브·시트 연동을 켰습니다.")
        except Exception as e:
            log.error("구글 드라이브 연동을 켜지 못했습니다 (봇은 드라이브 없이 계속 동작합니다): %s", e)
    else:
        log.info("GOOGLE_CREDENTIALS가 없어 구글 드라이브 연동 없이 동작합니다.")

    from .bot import ArchiveBot

    ArchiveBot(config, service).run(config.discord_token, log_handler=None)


if __name__ == "__main__":
    main()
