"""
mordomo-people — entry point.
Connects to NATS + Postgres + Redis and registers all subscriptions.
"""
import asyncio
import logging
import signal
import nats
from src import db, cache, handlers
from src.config import NATS_URL

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
)
logger = logging.getLogger("mordomo-people")

SUBJECTS = {
    "mordomo.people.resolve":         handlers.handle_resolve,
    "mordomo.people.permissions.get": handlers.handle_permissions_get,
    "mordomo.people.upsert":          handlers.handle_upsert,
}

_stop = asyncio.Event()


async def main() -> None:
    logger.info("Starting mordomo-people...")

    await db.init_pool()
    logger.info("PostgreSQL pool ready")

    await cache.init_redis()
    logger.info("Redis connection ready")

    nc = await nats.connect(
        NATS_URL,
        name="mordomo-people",
        reconnect_time_wait=2,
        max_reconnect_attempts=-1,  # retry forever
    )
    logger.info("NATS connected: %s", NATS_URL)

    for subject, handler in SUBJECTS.items():
        await nc.subscribe(subject, cb=handler)
        logger.info("Subscribed: %s", subject)

    def _shutdown(*_):
        logger.info("Shutdown signal received")
        _stop.set()

    loop = asyncio.get_event_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, _shutdown)

    logger.info("mordomo-people ready")
    await _stop.wait()

    logger.info("Shutting down...")
    await nc.drain()
    await db.close_pool()
    await cache.close_redis()
    logger.info("Bye.")


if __name__ == "__main__":
    asyncio.run(main())
