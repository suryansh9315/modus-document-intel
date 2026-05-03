"""
Workers entrypoint — keeps the container alive so the API can
trigger ingestion flows via asyncio.create_task().
"""
import asyncio
import logging
import signal

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


async def main():
    logger.info("Modus workers container running. Waiting for ingestion tasks...")
    stop = asyncio.Event()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)

    await stop.wait()
    logger.info("Workers shutting down.")


if __name__ == "__main__":
    asyncio.run(main())
