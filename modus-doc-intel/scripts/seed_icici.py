#!/usr/bin/env python3
"""
Seed script: upload the ICICI Bank Annual Report PDF and trigger ingestion.

Usage:
    python scripts/seed_icici.py [PDF_PATH] [API_URL]

Default PDF_PATH: ../ICICI Bank Report.pdf (relative to modus-doc-intel/ root)
Default API_URL: http://localhost:8000
"""
from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path

import httpx


DEFAULT_PDF = Path(__file__).parent.parent.parent / "ICICI Bank Report.pdf"
DEFAULT_API = "http://localhost:8000"


async def seed(pdf_path: Path, api_url: str) -> str:
    if not pdf_path.exists():
        print(f"ERROR: PDF not found at {pdf_path}")
        print(f"Usage: python seed_icici.py [PDF_PATH] [API_URL]")
        sys.exit(1)

    print(f"Uploading {pdf_path.name} ({pdf_path.stat().st_size / 1024 / 1024:.1f} MB)...")

    async with httpx.AsyncClient(base_url=api_url, timeout=120.0) as client:
        # Check API health
        health = await client.get("/health")
        health.raise_for_status()
        print(f"API is healthy: {health.json()}")

        # Upload PDF
        with open(pdf_path, "rb") as f:
            r = await client.post(
                "/ingestion/upload",
                files={"file": (pdf_path.name, f, "application/pdf")},
            )

        if r.status_code != 200:
            print(f"Upload failed ({r.status_code}): {r.text}")
            sys.exit(1)

        result = r.json()
        doc_id = result["doc_id"]
        print(f"\nUpload successful!")
        print(f"  Document ID: {doc_id}")
        print(f"  Status: {result['status']}")
        print(f"\nIngestion started. Polling for status...")
        print(f"This will take 30-45 minutes for a 341-page PDF.\n")

        # Poll status
        poll_interval = 15  # seconds
        while True:
            await asyncio.sleep(poll_interval)
            status_r = await client.get(f"/ingestion/{doc_id}")
            if status_r.status_code != 200:
                print(f"Status poll failed: {status_r.text}")
                continue

            job = status_r.json()
            status = job["status"]
            pct = job.get("progress_pct", 0)
            msg = job.get("message", "")
            error = job.get("error")

            print(f"  [{status}] {pct:.0f}% — {msg}")

            if status == "READY":
                print(f"\n✓ Ingestion complete! Document is ready for queries.")
                print(f"  Visit: http://localhost:3000/documents/{doc_id}")
                return doc_id
            elif status == "ERROR":
                print(f"\n✗ Ingestion failed: {error}")
                sys.exit(1)

            # Increase poll interval for long-running steps
            if status in ("ANALYZING", "AGGREGATING"):
                poll_interval = 30


if __name__ == "__main__":
    pdf_path = Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_PDF
    api_url = sys.argv[2] if len(sys.argv) > 2 else DEFAULT_API

    asyncio.run(seed(pdf_path, api_url))
