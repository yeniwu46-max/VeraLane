"""Safely evaluate a few synthetic DeepSeek intents without executing actions."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import tempfile
import time
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any


CASES: list[dict[str, Any]] = [
    {
        "case": "balance",
        "input": "我的账户余额是多少？",
        "expected": {"action": "balance_query"},
    },
    {
        "case": "bills",
        "input": "上个月餐饮花了多少？",
        "expected": {"action": "bill_summary", "period": "上个月"},
    },
    {
        "case": "transfer",
        "input": "转给林悦300元，备注房租",
        "expected": {
            "action": "transfer",
            "recipient": "林悦",
            "amount_yuan": "300",
            "note": "房租",
        },
    },
]


def fields_match(expected: dict[str, Any], actual: dict[str, Any]) -> bool:
    for field, wanted in expected.items():
        observed = actual.get(field)
        if field == "amount_yuan":
            try:
                if Decimal(str(observed)) != Decimal(str(wanted)):
                    return False
            except (InvalidOperation, TypeError):
                return False
        elif observed != wanted:
            return False
    return True


async def evaluate(limit: int) -> dict[str, Any]:
    # Imports happen only after the isolated DB path and request caps are set.
    backend_root = Path(__file__).resolve().parents[1] / "backend"
    sys.path.insert(0, str(backend_root))
    from app.agent import parse_intent
    from app.model_budget import get_model_status

    rows: list[dict[str, Any]] = []
    for case in CASES[:limit]:
        started = time.perf_counter()
        try:
            parsed = await parse_intent(case["input"], ["林悦"], ["云影会员"])
        except Exception as exc:  # Never print exception text that could contain request data.
            rows.append({"case": case["case"], "mode": "local_error", "error_type": type(exc).__name__})
            break

        actual = parsed.intent.model_dump(exclude_none=True)
        row: dict[str, Any] = {
            "case": case["case"],
            "mode": parsed.mode,
            "latency_ms": round((time.perf_counter() - started) * 1000),
            "fallback_reason": (parsed.metadata or {}).get("fallback_reason"),
            "http_status": (parsed.metadata or {}).get("http_status"),
            "usage": parsed.usage,
        }
        if parsed.mode == "deepseek":
            row["actual_intent"] = actual
            row["expected_fields_match"] = fields_match(case["expected"], actual)
        else:
            # A rule fallback may look correct but is not model evidence.
            row["fallback_intent"] = actual
            row["expected_fields_match"] = None
        rows.append(row)

        # Avoid spending the rest of the limit on a broken credential or endpoint.
        if parsed.mode != "deepseek":
            break

    status = get_model_status()
    verified = [row for row in rows if row.get("mode") == "deepseek"]
    return {
        "scope": "fixed synthetic intent samples only; no account data, banking tools, or action execution",
        "requests_sent": status.get("usage", {}).get("attempts", 0),
        "successful_model_responses": len(verified),
        "field_matches": sum(row.get("expected_fields_match") is True for row in verified),
        "cases": rows,
        "usage": status.get("usage"),
        "limits": status.get("limits"),
        "model": status.get("model"),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", help="show fixed samples and limits; never use the network (default)")
    mode.add_argument("--allow-network", action="store_true", help="permit direct calls to the configured DeepSeek model")
    parser.add_argument("--max-requests", type=int, choices=(1, 2, 3), default=None,
                        help="explicit request ceiling for this run (maximum 3)")
    args = parser.parse_args()

    if not args.allow_network:
        print(json.dumps({"network_enabled": False, "max_requests": 0,
                          "samples": [{"case": case["case"], "input": case["input"]} for case in CASES]},
                         ensure_ascii=True))
        return 0
    if args.max_requests is None:
        parser.error("--allow-network requires --max-requests 1, 2, or 3")
    if not os.environ.get("DEEPSEEK_API_KEY", "").strip():
        parser.error("DEEPSEEK_API_KEY is missing; no request was sent")

    # Bound this one-shot run independently of looser settings in .env.
    os.environ.update({
        "VERALANE_MODEL_MODE": "auto",
        "DEEPSEEK_MAX_CALLS": str(args.max_requests),
        "DEEPSEEK_MAX_INFLIGHT": "1",
        "DEEPSEEK_MAX_OUTPUT_TOKENS": "384",
        "DEEPSEEK_MAX_TOTAL_TOKENS": "6000",
        "DEEPSEEK_TIMEOUT_SECONDS": "15",
        "DEEPSEEK_BUDGET_YUAN": "20",
    })
    with tempfile.TemporaryDirectory(prefix="veralane-intent-eval-") as temp_dir:
        os.environ["VERALANE_DB_PATH"] = str(Path(temp_dir) / "usage.sqlite3")
        result = asyncio.run(evaluate(args.max_requests))
    print(json.dumps(result, ensure_ascii=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
