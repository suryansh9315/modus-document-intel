"""
FastAPI application entry point.
Includes lifespan management for MongoDB connection and DuckDB initialization.
"""
from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager

import motor.motor_asyncio
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from modus_api.config import settings
from modus_api.routes import documents, ingestion, queries
from modus_workers.tasks.duckdb_write import init_schema as init_duckdb

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(name)s %(levelname)s %(message)s",
)
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup: connect MongoDB, init DuckDB schema. Shutdown: close connections."""
    # MongoDB
    logger.info(f"Connecting to MongoDB: {settings.mongo_uri}")
    mongo_client = motor.motor_asyncio.AsyncIOMotorClient(settings.mongo_uri)
    app.state.db = mongo_client[settings.mongo_db_name]
    app.state.mongo_client = mongo_client

    # DuckDB schema init
    os.makedirs(os.path.dirname(settings.duckdb_path), exist_ok=True)
    try:
        init_duckdb(settings.duckdb_path)
        logger.info(f"DuckDB ready at {settings.duckdb_path}")
    except Exception as e:
        logger.warning(f"DuckDB init skipped (non-fatal): {e}")

    # Upload directory
    os.makedirs(settings.upload_dir, exist_ok=True)

    logger.info("Application startup complete.")
    yield

    # Cleanup
    mongo_client.close()
    logger.info("Application shutdown complete.")


app = FastAPI(
    title="Modus Document Intelligence API",
    description="Multi-agent document intelligence for financial reports",
    version="0.1.0",
    lifespan=lifespan,
)

# CORS — allow_origins=["*"] is incompatible with allow_credentials=True (browser spec).
# Use allow_origin_regex to permit any localhost port for local development while
# still supporting explicit prod origins from settings.
app.add_middleware(
    CORSMiddleware,
    allow_origins=[o.strip() for o in settings.cors_origins.split(",") if o.strip()],
    allow_origin_regex=r"http://localhost(:\d+)?",
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Include routers
app.include_router(documents.router, prefix="/documents", tags=["Documents"])
app.include_router(ingestion.router, prefix="/ingestion", tags=["Ingestion"])
app.include_router(queries.router, prefix="/queries", tags=["Queries"])


@app.get("/health")
async def health_check():
    return {"status": "ok", "version": "0.1.0"}
