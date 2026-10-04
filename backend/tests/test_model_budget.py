from __future__ import annotations

import asyncio
import json
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from decimal import Decimal

import httpx
import pytest

from app import agent, db, model_budget as budget


@pytest.fixture(autouse=True)
def isolated_accounting(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "model.sqlite3")
    for name in (
        "DEEPSEEK_API_KEY", "DEEPSEEK_MODEL", "DEEPSEEK_MAX_OUTPUT_TOKENS",
        "DEEPSEEK_TIMEOUT_SECONDS", "DEEPSEEK_MAX_CALLS", "DEEPSEEK_MAX_TOTAL_TOKENS",
        "DEEPSEEK_MAX_INFLIGHT", "DEEPSEEK_BUDGET_YUAN",
        "DEEPSEEK_INPUT_PRICE_PER_MILLION_YUAN", "DEEPSEEK_OUTPUT_PRICE_PER_MILLION_YUAN",
    ):
        monkeypatch.delenv(name, raising=False)


def messages():
    return [{"role": "user", "content": "我的余额"}]


def rows():
    conn = db.connect()
    try:
        return [dict(row) for row in conn.execute("SELECT * FROM model_attempts")]
    finally:
        conn.close()


def mock_model(monkeypatch, payload=None, error=None, seen=None):
    class Response:
        def raise_for_status(self):
            pass

        def json(self):
            return payload

    class Client:
        def __init__(self, **kwargs):
            if seen is not None:
                seen["client"] = kwargs

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def post(self, *args, **kwargs):
            if seen is not None:
                seen["request"] = kwargs
            if error:
                raise error
            return Response()

    monkeypatch.setenv("DEEPSEEK_API_KEY", "secret-never-store")
    monkeypatch.setattr(agent.httpx, "AsyncClient", Client)


def payload(usage=None, content='{"action":"balance_query"}'):
    return {"choices": [{"message": {"content": content}}], "usage": usage}


def parse(message="账户余额"):
    return asyncio.run(agent.parse_intent(message, ["林悦"], []))


def test_no_key_uses_rules_without_making_accounting_claims():
    result = parse()
    assert result.mode == "offline" and result.usage is None
    assert result.metadata["fallback_reason"] == "missing_api_key"
    status = budget.get_model_status()
    assert status["usage"]["attempts"] == 0
    assert status["usage"]["estimated_cost_yuan"] is None


def test_atomic_call_limit_allows_only_one_concurrent_reservation():
    config = replace(budget.load_config(), max_calls=1, max_inflight=8)
    with ThreadPoolExecutor(max_workers=8) as pool:
        attempts = list(pool.map(lambda _: budget.reserve_call(config, messages()), range(8)))
    assert sum(item.attempt_id is not None for item in attempts) == 1
    assert [item.reason for item in attempts].count("call_limit") == 7
    assert budget.get_model_status()["usage"]["attempts"] == 1


def test_atomic_token_limit_reserves_future_output_too():
    base = budget.load_config()
    needed = budget.prompt_reservation(messages()) + base.max_output_tokens
    config = replace(base, max_total_tokens=needed, max_inflight=8)
    with ThreadPoolExecutor(max_workers=6) as pool:
        attempts = list(pool.map(lambda _: budget.reserve_call(config, messages()), range(6)))
    assert sum(item.attempt_id is not None for item in attempts) == 1
    assert [item.reason for item in attempts].count("token_limit") == 5
    assert budget.get_model_status()["usage"]["accounted_tokens"] == needed


def test_concurrency_slot_released_after_finish_but_call_still_counted():
    config = replace(budget.load_config(), max_inflight=1)
    first = budget.reserve_call(config, messages())
    assert budget.reserve_call(config, messages()).reason == "concurrency_limit"
    budget.finish_call(first.attempt_id, succeeded=True, usage={"prompt_tokens": 10, "completion_tokens": 3})
    assert budget.reserve_call(config, messages()).attempt_id
    assert budget.get_model_status()["usage"]["attempts"] == 2


def test_budget_reservation_blocks_concurrent_spend_at_configured_prices():
    base = budget.load_config()
    needed = budget.prompt_reservation(messages()) + base.max_output_tokens
    config = replace(base, input_price=Decimal("1"), output_price=Decimal("1"),
                     budget_yuan=Decimal(needed) / 1_000_000, max_inflight=8)
    with ThreadPoolExecutor(max_workers=4) as pool:
        attempts = list(pool.map(lambda _: budget.reserve_call(config, messages()), range(4)))
    assert sum(item.attempt_id is not None for item in attempts) == 1
    assert [item.reason for item in attempts].count("budget_limit") == 3


def test_failures_keep_unknown_usage_reservation_and_never_store_secrets(monkeypatch):
    mock_model(monkeypatch, error=httpx.ConnectError("secret-never-store and user text"))
    result = parse("账户余额，备注私密内容")
    assert result.mode == "offline" and result.usage is None
    assert result.metadata["fallback_reason"] == "model_http_error"
    row = rows()[0]
    assert row["status"] == "failed" and row["prompt_tokens"] is None
    assert row["accounted_tokens"] > 0
    serialized = json.dumps([row, budget.get_model_status(), result.metadata], ensure_ascii=False)
    assert "secret-never-store" not in serialized and "私密内容" not in serialized


@pytest.mark.parametrize("usage", [None, {}, {"prompt_tokens": 5},
    {"prompt_tokens": "5", "completion_tokens": 1},
    {"prompt_tokens": True, "completion_tokens": 1},
    {"prompt_tokens": -1, "completion_tokens": 1}])
def test_missing_or_invalid_provider_usage_is_unknown_not_zero(monkeypatch, usage):
    mock_model(monkeypatch, payload(usage))
    result = parse()
    assert result.mode == "deepseek" and result.usage is None
    assert budget.get_model_status()["usage"]["uncertain_attempts"] == 1
    assert rows()[0]["accounted_tokens"] > 768


def test_valid_usage_replaces_reservation_without_double_counting(monkeypatch):
    mock_model(monkeypatch, payload({"prompt_tokens": 40, "completion_tokens": 12}))
    result = parse()
    row = rows()[0]
    assert result.usage == {"prompt_tokens": 40, "completion_tokens": 12}
    budget.finish_call(row["id"], succeeded=True, usage={"prompt_tokens": 100, "completion_tokens": 100})
    status = budget.get_model_status()
    assert status["usage"]["attempts"] == 1
    assert status["usage"]["accounted_tokens"] == status["usage"]["reported_tokens"] == 52
    conn = db.connect()
    try:
        assert conn.execute("SELECT COUNT(*) FROM sqlite_master WHERE name='model_usage'").fetchone()[0] == 0
    finally:
        conn.close()


def test_bad_json_still_accounts_reported_usage(monkeypatch):
    mock_model(monkeypatch, payload({"prompt_tokens": 40, "completion_tokens": 12}, "{"))
    result = parse()
    assert result.mode == "offline" and result.intent.action == "balance_query"
    assert result.metadata["fallback_reason"] == "invalid_model_response"
    assert rows()[0]["accounted_tokens"] == 52


def test_timeout_is_recorded_and_falls_back(monkeypatch):
    mock_model(monkeypatch, error=httpx.ReadTimeout("key or body must not be copied"))
    result = parse("转给林悦300元")
    assert result.intent.action == "transfer" and result.intent.amount_yuan == "300"
    assert result.metadata["fallback_reason"] == "model_timeout"
    assert rows()[0]["status"] == "failed"


def test_runtime_configuration_is_used_and_safe(monkeypatch):
    seen = {}
    mock_model(monkeypatch, payload({"prompt_tokens": 10, "completion_tokens": 10}), seen=seen)
    monkeypatch.setenv("DEEPSEEK_MODEL", "deepseek-v4-pro")
    monkeypatch.setenv("DEEPSEEK_TIMEOUT_SECONDS", "7")
    monkeypatch.setenv("DEEPSEEK_MAX_OUTPUT_TOKENS", "1024")
    assert parse().mode == "deepseek"
    assert seen["request"]["json"]["model"] == "deepseek-v4-pro"
    assert seen["request"]["json"]["max_tokens"] == 1024
    assert seen["request"]["json"]["thinking"] == {"type": "disabled"}
    assert seen["client"]["timeout"] == 7


@pytest.mark.parametrize("name,value", [
    ("DEEPSEEK_TIMEOUT_SECONDS", "NaN"), ("DEEPSEEK_MAX_CALLS", "-1"),
    ("DEEPSEEK_MAX_OUTPUT_TOKENS", "99999999"), ("DEEPSEEK_MODEL", "bad\nname"),
    ("DEEPSEEK_INPUT_PRICE_PER_MILLION_YUAN", "2"),
])
def test_invalid_configuration_fails_closed_without_network(monkeypatch, name, value):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-secret")
    monkeypatch.setenv(name, value)
    result = parse()
    assert result.mode == "offline" and result.metadata["fallback_reason"] == "invalid_model_config"
    assert budget.get_model_status()["reason"] == "invalid_model_config"


def test_disabled_call_limit_never_creates_client(monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-secret")
    monkeypatch.setenv("DEEPSEEK_MAX_CALLS", "0")
    monkeypatch.setattr(agent.httpx, "AsyncClient", lambda **kw: pytest.fail("unexpected network call"))
    result = parse()
    assert result.metadata["fallback_reason"] == "call_limit"
    assert budget.get_model_status()["mode"] == "offline"
    assert rows()[0]["status"] == "blocked"


def test_restart_expiry_frees_slot_but_keeps_uncertain_spend(monkeypatch):
    config = replace(budget.load_config(), max_inflight=1)
    reserved = budget.reserve_call(config, messages())
    before = rows()[0]["accounted_tokens"]
    conn = db.connect()
    try:
        conn.execute("UPDATE model_attempts SET lease_until=0 WHERE id=?", (reserved.attempt_id,))
    finally:
        conn.close()
    assert budget.reserve_call(config, messages()).attempt_id
    assert rows()[0]["status"] == "unknown"
    assert rows()[0]["accounted_tokens"] == before
    # A late provider result reconciles the existing reservation, never a new call.
    budget.finish_call(reserved.attempt_id, succeeded=True, usage={"prompt_tokens": 4, "completion_tokens": 5})
    assert rows()[0]["accounted_tokens"] == 9


def test_adding_prices_cannot_hide_historical_unpriced_calls(monkeypatch):
    config = budget.load_config()
    attempt = budget.reserve_call(config, messages())
    budget.finish_call(attempt.attempt_id, succeeded=True, usage={"prompt_tokens": 4, "completion_tokens": 5})
    priced = replace(config, input_price=Decimal("1"), output_price=Decimal("2"))
    assert budget.reserve_call(priced, messages()).reason == "pricing_history_unknown"


def test_estimated_price_is_not_provider_account_balance(monkeypatch):
    monkeypatch.setenv("DEEPSEEK_INPUT_PRICE_PER_MILLION_YUAN", "1")
    monkeypatch.setenv("DEEPSEEK_OUTPUT_PRICE_PER_MILLION_YUAN", "2")
    mock_model(monkeypatch, payload({"prompt_tokens": 100, "completion_tokens": 50}))
    parse()
    status = budget.get_model_status()
    assert status["usage"]["estimated_cost_yuan"] == "0.000200"
    assert status["limits"]["budget_yuan"] == "20"
    assert "已使用多少未知" in status["notice"]


def test_zero_budget_blocks_even_without_prices(monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-secret")
    monkeypatch.setenv("DEEPSEEK_BUDGET_YUAN", "0")
    result = parse()
    assert result.metadata["fallback_reason"] == "budget_limit"
    assert budget.get_model_status()["reason"] == "budget_limit"


def test_truncated_completion_is_not_trusted_even_if_json_is_parseable(monkeypatch):
    response = payload({"prompt_tokens": 20, "completion_tokens": 10})
    response["choices"][0]["finish_reason"] = "length"
    mock_model(monkeypatch, response)
    result = parse("转给林悦300元")
    assert result.mode == "offline" and result.intent.action == "transfer"
    assert result.metadata["fallback_reason"] == "invalid_model_response"
    assert rows()[0]["accounted_tokens"] == 30
