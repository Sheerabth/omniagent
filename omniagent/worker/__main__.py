"""Worker entry point: python -m omniagent.worker"""

import asyncio
import logging

from omniagent.config import settings
from omniagent.logging_config import configure_logging

configure_logging()
logger = logging.getLogger(__name__)


async def main() -> None:
    from procrastinate.worker import Worker

    from omniagent.db import close_db, init_db
    from omniagent.migrations import run_migrations
    from omniagent.worker.job import app, run_agent_job  # noqa: F401 — registers task
    from omniagent.worker.scheduler import check_schedules  # noqa: F401 — registers periodic task

    await run_migrations(settings.database_url)
    await init_db(settings.database_url)

    concurrency = settings.worker_concurrency
    logger.info(
        "Worker starting, polling queue '%s', concurrency=%d",
        settings.worker_queue_name,
        concurrency,
    )
    async with app.open_async():
        worker = Worker(app, queues=[settings.worker_queue_name], concurrency=concurrency)
        await worker.run()

    await close_db()


if __name__ == "__main__":
    asyncio.run(main())
