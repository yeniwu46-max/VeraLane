from __future__ import annotations

import asyncio
import json

import pytest

from app import agent
from app.aa_intent import parse_aa_message, wants_aa
from app.agent import offline_intent


CONTACTS = ["林悦", "王明", "王明", "陈晨"]


def test_main_aa_example_extracts_explicit_slots_without_computing_shares():
    intent = offline_intent("聚餐我垫了368.50元，我、林悦、王明、陈晨四个人AA", CONTACTS, [])
    assert intent.action == "aa_split"
    assert intent.amount_yuan == "368.50"
    assert intent.participants == ["林悦", "王明", "陈晨"]
    assert intent.include_self is True
    assert intent.participant_count == 4
    assert intent.payer_is_self is True
    assert intent.note == "聚餐"
    assert intent.aa_non_equal is False


@pytest.mark.parametrize("message,participants,self_included", [
    ("我付了100元，和林悦、陈晨AA", ["林悦", "陈晨"], True),
    ("让林悦陈晨分摊100元", ["林悦", "陈晨"], None),
    ("我垫付100元，林悦、陈晨AA", ["林悦", "陈晨"], None),
    ("我和林悦、张三AA100元", ["林悦", "张三"], True),
    ("我和林悦张三AA100元", ["林悦", "张三"], True),
    ("林悦、陈晨AA100元，不包括我", ["林悦", "陈晨"], False),
    ("我和林悦、林悦AA100元", ["林悦", "林悦"], True),
    ("林悦和我平摊100元", ["林悦"], True),
    ("我和13900001111均分100元", ["13900001111"], True),
])
def test_participant_mentions_are_conservative(message, participants, self_included):
    value = parse_aa_message(message, CONTACTS)
    assert value["participants"] == participants
    assert value["include_self"] is self_included


@pytest.mark.parametrize("message,expected", [
    ("包括我", {"include_self": True}),
    ("不包括我", {"include_self": False}),
    ("四个人", {"participant_count": 4}),
    ("一共3人", {"participant_count": 3}),
    ("300元", {"amount_yuan": "300"}),
    ("林悦、陈晨", {"participants": ["林悦", "陈晨"]}),
    ("备注聚餐", {"note": "聚餐", "participants": None}),
])
def test_partial_replies_do_not_require_aa_keyword(message, expected):
    value = parse_aa_message(message, CONTACTS)
    for key, item in expected.items():
        assert value[key] == item


@pytest.mark.parametrize("message,expected", [
    ("我垫付100元，林悦和我AA", True),
    ("我自己垫付100元，林悦和我AA", True),
    ("林悦垫付100元，林悦和我AA", False),
    ("不是我垫付100元，林悦和我AA", False),
    ("林悦和我AA100元", None),
])
def test_payer_is_separate_from_cost_participation(message, expected):
    assert parse_aa_message(message, CONTACTS)["payer_is_self"] is expected


@pytest.mark.parametrize("amount,expected", [
    ("368.50", "368.50"), ("0.01", "0.01"), ("-100", "-100"),
    ("300.123", "300.123"), ("1,000", None), ("1e3", None),
])
def test_money_token_is_not_rounded_or_partially_recovered(amount, expected):
    assert parse_aa_message(f"我和林悦AA{amount}元", CONTACTS)["amount_yuan"] == expected


@pytest.mark.parametrize("message", [
    "我和林悦AA100元，林悦少付20元",
    "我和林悦按比例分摊100元",
    "我和林悦分摊100元，每人付50元",
    "我和林悦AA100元，加上20元",
])
def test_custom_or_multiple_amounts_require_manual_allocation(message):
    assert parse_aa_message(message, CONTACTS)["aa_non_equal"] is True


@pytest.mark.parametrize("message,weights,participants", [
    ("聚餐我垫了120元，我一份，林悦两份，陈晨一份AA", [{"name": "我", "weight": 1, "kind": "units"}, {"name": "林悦", "weight": 2, "kind": "units"}, {"name": "陈晨", "weight": 1, "kind": "units"}], ["林悦", "陈晨"]),
    ("我和林悦按比例分摊100元，我:1，林悦:3", [{"name": "我", "weight": 1, "kind": "ratio"}, {"name": "林悦", "weight": 3, "kind": "ratio"}], ["林悦"]),
    ("我承担20%，林悦30%，陈晨50%AA", [{"name": "我", "weight": 20, "kind": "percentage"}, {"name": "林悦", "weight": 30, "kind": "percentage"}, {"name": "陈晨", "weight": 50, "kind": "percentage"}], ["林悦", "陈晨"]),
    ("我百分之二十，林悦百分之三十，陈晨百分之五十AA", [{"name": "我", "weight": 20, "kind": "percentage"}, {"name": "林悦", "weight": 30, "kind": "percentage"}, {"name": "陈晨", "weight": 50, "kind": "percentage"}], ["林悦", "陈晨"]),
    ("我百分之百，林悦百分之零AA", [{"name": "我", "weight": 100, "kind": "percentage"}, {"name": "林悦", "weight": 0, "kind": "percentage"}], ["林悦"]),
])
def test_explicit_named_weights_are_extracted_without_computing_amounts(message, weights, participants):
    value = parse_aa_message(message, CONTACTS)
    assert value["share_weights"] == weights
    assert value["participants"] == participants
    assert value["aa_non_equal"] is True


def test_partial_or_ambiguous_weight_specification_is_not_completed_by_the_parser():
    value = parse_aa_message("我一份，王明两份，林悦AA100元", CONTACTS)
    assert value["share_weights"] == [{"name": "我", "weight": 1, "kind": "units"}, {"name": "王明", "weight": 2, "kind": "units"}]
    assert value["participants"] == ["王明", "林悦"]


def test_unsupported_decimal_percentages_still_require_custom_share_review():
    value = parse_aa_message("我12.5%，林悦87.5%，我们AA", CONTACTS)
    assert value["share_weights"] is None
    assert value["aa_non_equal"] is True


def test_note_is_data_and_does_not_add_participants_or_constraints():
    value = parse_aa_message("我和林悦AA100元，备注陈晨少付20元，不包括我", CONTACTS)
    assert value["participants"] == ["林悦"]
    assert value["include_self"] is True
    assert value["amount_yuan"] == "100"
    assert value["aa_non_equal"] is False
    assert not wants_aa("转给林悦100元，备注AA聚餐")
    assert offline_intent("转给林悦100元，备注AA聚餐", CONTACTS, []).action == "transfer"


def test_aa_precedes_bill_and_transfer_words():
    assert offline_intent("聚餐账单100元，我和林悦AA", CONTACTS, []).action == "aa_split"


def test_local_aa_override_reports_rules_source_and_keeps_model_usage(monkeypatch):
    class MockResponse:
        def raise_for_status(self):
            pass

        def json(self):
            return {
                "choices": [{"message": {"content": json.dumps({
                    "action": "aa_split", "amount_yuan": "999",
                    "participants": ["陈晨"], "include_self": False,
                })}}],
                "usage": {"prompt_tokens": 100, "completion_tokens": 20},
            }

    class MockClient:
        def __init__(self, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def post(self, *args, **kwargs):
            return MockResponse()

    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-key")
    monkeypatch.setenv("VERALANE_MODEL_MODE", "auto")
    monkeypatch.setattr(agent.httpx, "AsyncClient", MockClient)
    result = asyncio.run(agent.parse_intent("我和林悦AA100元", CONTACTS, []))
    assert result.mode == "offline"
    assert result.intent.amount_yuan == "100"
    assert result.intent.participants == ["林悦"]
    assert result.intent.include_self is True
    assert result.usage == {"prompt_tokens": 100, "completion_tokens": 20}
