import asyncio
import logging
import signal
import nats
import uvicorn
from src import db, cache, handlers
from src.config import NATS_URL, HTTP_HOST, HTTP_PORT
from src.dashboard import app

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

async def run_nats():
    """Background task to handle NATS messages."""
    nc = await nats.connect(
        NATS_URL,
        name="mordomo-people",
        reconnect_time_wait=2,
        max_reconnect_attempts=-1,
    )
    logger.info("NATS connected: %s", NATS_URL)

    for subject, handler in SUBJECTS.items():
        await nc.subscribe(subject, cb=handler)
        logger.info("Subscribed: %s", subject)
    
    return nc

async def main() -> None:
    logger.info("Starting mordomo-people (Web + NATS)...")

    await db.init_pool()
    logger.info("PostgreSQL pool ready")

    await cache.init_redis()
    logger.info("Redis connection ready")

    # 1. Start NATS in background
    nc = await run_nats()
    app.state.nc = nc
    app.state.redis = cache.redis_client

    # 2. Configure and Start FastAPI (Uvicorn)
    config = uvicorn.Config(
        app, 
        host=HTTP_HOST, 
        port=HTTP_PORT, 
        log_level="info"
    )
    server = uvicorn.Server(config)

    # Handle termination signals
    _stop = asyncio.Event()
    def _shutdown(*_):
        logger.info("Shutdown signal received")
        _stop.set()
        server.should_exit = True

    loop = asyncio.get_event_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, _shutdown)

    # Run Uvicorn server in the current event loop
    # This will block until the server exits
    await server.serve()

    logger.info("Shutting down...")
    await nc.drain()
    await db.close_pool()
    await cache.close_redis()
    logger.info("Bye.")


if __name__ == "__main__":
    asyncio.run(main())
