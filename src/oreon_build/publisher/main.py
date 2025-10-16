"""Publisher daemon: composes repositories and uploads to R2 (no local repo storage)."""
import asyncio
import logging
import sys

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
logger = logging.getLogger(__name__)


def main() -> None:
    logger.info("Oreon publisher: repository composition is triggered via API or scheduler")
    sys.exit(0)


if __name__ == "__main__":
    main()
