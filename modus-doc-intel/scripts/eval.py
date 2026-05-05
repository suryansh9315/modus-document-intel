#!/usr/bin/env python3
"""
Evaluation script: run golden Q&A pairs against the API and report results.

Usage:
    python scripts/eval.py [DOC_ID] [API_URL]

Outputs:
    - Per-question: PASS/FAIL based on keyword presence
    - Summary: accuracy percentage
    - Detailed results saved to eval/results_{timestamp}.json
"""
from __future__ import annotations

import asyncio
import json
import sys
import time
from datetime import datetime
from pathlib import Path

import httpx
import yaml

EVAL_DIR = Path(__file__).parent.parent / "eval"
GOLDEN_FILE = EVAL_DIR / "golden_questions.yaml"
DEFAULT_API = "http://localhost:8000"


async def run_query(
    client: httpx.AsyncClient,
    doc_id: str,
    query_type: str,
    question: str,
    section_ids: list[str] | None = None,
) -> str:
    """Run a single query (non-streaming) and return the answer."""
    body = {
        "doc_id": doc_id,
        "query_type": query_type,
        "question": question,
        "stream": False,
    }
    if section_ids:
        body["section_ids"] = section_ids

    r = await client.post("/queries/", json=body, timeout=120.0)
    if r.status_code != 200:
        raise ValueError(f"Query failed ({r.status_code}): {r.text}")

    data = r.json()
    return data.get("answer", "")


def evaluate_answer(answer: str, expected_keywords: list[str]) -> tuple[bool, list[str]]:
    """Check if expected keywords appear in the answer (case-insensitive)."""
    answer_lower = answer.lower()
    missing = [kw for kw in expected_keywords if kw.lower() not in answer_lower]
    return len(missing) == 0, missing


async def run_evaluation(doc_id: str, api_url: str) -> dict:
    """Run all golden questions and collect results."""
    with open(GOLDEN_FILE) as f:
        data = yaml.safe_load(f)

    questions = data["golden_questions"]
    results = []
    passed = 0
    failed = 0
    errors = 0

    async with httpx.AsyncClient(base_url=api_url, timeout=120.0) as client:
        # Verify API health
        health = await client.get("/health")
        health.raise_for_status()
        print(f"API healthy. Evaluating {len(questions)} questions against doc {doc_id}...\n")

        for i, q in enumerate(questions):
            qid = q["id"]
            query_type = q["query_type"]
            question = q["question"]
            expected_kw = q.get("expected_keywords", [])
            notes = q.get("notes", "")

            print(f"[{i+1:2d}/{len(questions)}] {qid}: {question[:60]}...")

            try:
                start = time.monotonic()
                answer = await run_query(
                    client, doc_id, query_type, question,
                    section_ids=q.get("section_ids"),
                )
                elapsed = time.monotonic() - start

                ok, missing = evaluate_answer(answer, expected_kw)
                status = "PASS" if ok else "FAIL"

                if ok:
                    passed += 1
                    print(f"  ✓ {status} ({elapsed:.1f}s)")
                else:
                    failed += 1
                    print(f"  ✗ {status} ({elapsed:.1f}s) — missing: {missing}")

                results.append({
                    "id": qid,
                    "query_type": query_type,
                    "question": question,
                    "status": status,
                    "elapsed_s": round(elapsed, 2),
                    "answer_length": len(answer),
                    "missing_keywords": missing,
                    "answer_preview": answer[:300],
                    "notes": notes,
                })

            except Exception as e:
                errors += 1
                print(f"  ! ERROR: {e}")
                results.append({
                    "id": qid,
                    "query_type": query_type,
                    "question": question,
                    "status": "ERROR",
                    "error": str(e),
                    "notes": notes,
                })

            # Delay between questions — each query makes 2-4 LLM calls.
            # Cerebras limit is 30 req/min, so 10s gives adequate headroom.
            await asyncio.sleep(10)

    total = len(questions)
    accuracy = passed / total * 100 if total > 0 else 0

    summary = {
        "timestamp": datetime.utcnow().isoformat(),
        "doc_id": doc_id,
        "api_url": api_url,
        "total": total,
        "passed": passed,
        "failed": failed,
        "errors": errors,
        "accuracy_pct": round(accuracy, 1),
        "results": results,
    }

    # Save results
    out_path = EVAL_DIR / f"results_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
    with open(out_path, "w") as f:
        json.dump(summary, f, indent=2)

    print(f"\n{'='*60}")
    print(f"EVALUATION RESULTS")
    print(f"{'='*60}")
    print(f"  Total:    {total}")
    print(f"  Passed:   {passed} ({accuracy:.1f}%)")
    print(f"  Failed:   {failed}")
    print(f"  Errors:   {errors}")
    print(f"  Results:  {out_path}")
    print(f"{'='*60}")

    return summary


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python eval.py DOC_ID [API_URL]")
        print("  DOC_ID: the document ID returned by seed_icici.py")
        sys.exit(1)

    doc_id = sys.argv[1]
    api_url = sys.argv[2] if len(sys.argv) > 2 else DEFAULT_API

    asyncio.run(run_evaluation(doc_id, api_url))
