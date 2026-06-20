"""Worker entry point: python -m omniagent.worker"""

import asyncio
import logging
import os
import sys

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


async def main() -> None:
    if not os.environ.get("DATABASE_URL"):
        logger.error("DATABASE_URL not set")
        sys.exit(1)

    from procrastinate.worker import Worker

    from omniagent.control_plane.db import close_pool, init_pool
    from omniagent.control_plane.migrations import run_migrations
    from omniagent.worker.job import app, run_agent_job  # noqa: F401 — registers task
    from omniagent.worker.scheduler import check_schedules  # noqa: F401 — registers periodic task

    await run_migrations(os.environ["DATABASE_URL"])
    await init_pool()

    logger.info("Worker starting, polling queue 'default'")
    async with app.open_async():
        worker = Worker(app, queues=["default"])
        await worker.run()

    await close_pool()


if __name__ == "__main__":
    asyncio.run(main())
