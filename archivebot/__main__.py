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
    log = logging.getLogger("archivebot")
    config = Config.from_env()
    if not config.discord_token:
        sys.exit("DISCORD_TOKEN이 없습니다. .env.example을 .env로 복사해 토큰을 넣어 주세요.")

    analyzer = None
    if config.anthropic_api_key:
        from .llm import ClaudeAnalyzer

        analyzer = ClaudeAnalyzer(config.anthropic_api_key, config.model)
        log.info("AI 분류: %s", config.model)
    else:
        log.warning("ANTHROPIC_API_KEY가 없어 키워드 기반 간이 분류로 동작합니다 (모든 자료에 확인 버튼이 뜹니다).")

    config.archive_dir.mkdir(parents=True, exist_ok=True)
    service = ArchiveService(config, Database(config.db_path), analyzer)

    from .bot import ArchiveBot

    ArchiveBot(config, service).run(config.discord_token, log_handler=None)


if __name__ == "__main__":
    main()
