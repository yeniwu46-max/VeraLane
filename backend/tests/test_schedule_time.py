from datetime import datetime

import pytest

from app.agent import extract_amount_text, offline_intent
from app.clock import SHANGHAI
from app.schedule_time import parse_schedule, wants_schedule


NOW = datetime(2026, 10, 3, 10, 0, tzinfo=SHANGHAI)


@pytest.mark.parametrize(("text", "expected"), [
    ("今天20:00转给林悦300元", "2026-10-03T20:00:00+08:00"),
    ("明天晚上八点半转给林悦300元", "2026-10-04T20:30:00+08:00"),
    ("后天上午十点二十五分转给林悦300元", "2026-10-05T10:25:00+08:00"),
    ("10月6日20:05转给林悦300元", "2026-10-06T20:05:00+08:00"),
    ("2027/1/2 00:00转给林悦300元", "2027-01-02T00:00:00+08:00"),
    ("2026-10-06 23:59转给林悦300元", "2026-10-06T23:59:00+08:00"),
    ("明天中午12点转给林悦300元", "2026-10-04T12:00:00+08:00"),
    ("明天凌晨0点转给林悦300元", "2026-10-04T00:00:00+08:00"),
])
def test_exact_dates_and_times(text, expected):
    result = parse_schedule(text, NOW)
    assert result["execute_at"] == expected
    assert "error" not in result


@pytest.mark.parametrize("text", [
    "明天转给林悦300元", "下午转给林悦300元", "明天晚上转给林悦300元",
    "下周转给林悦300元", "晚点转给林悦300元", "月底转给林悦300元",
    "周五转给林悦300元", "生日转给林悦300元", "过几天转给林悦300元",
    "一小时后转给林悦300元", "每月6日20:00转给林悦300元",
    "明天晚上12点转给林悦300元", "明天上午12点转给林悦300元",
    "明天中午1点转给林悦300元", "明天晚上1点转给林悦300元",
    "明天上午晚上8点转给林悦300元", "明天后天20:00转给林悦300元",
    "10月6日或7日20:00转给林悦300元", "明天20:00或21:0转给林悦300元",
    "明天20:00到21:00转给林悦300元", "明天20:00左右转给林悦300元",
    "明天晚上8点一刻转给林悦300元", "明天20:00:30转给林悦300元",
    "10月32日20:00转给林悦300元", "20266年10月6日20:00转给林悦300元",
    "明天24:00转给林悦300元", "明天20:60转给林悦300元",
    "明天8点转给林悦300元", "明天立即转给林悦300元",
])
def test_uncertain_future_intent_never_becomes_immediate(text):
    assert wants_schedule(text)
    result = parse_schedule(text, NOW)
    assert result is not None
    assert result.get("error")
    assert "execute_at" not in result


@pytest.mark.parametrize("when", ["下个月", "下个季度", "大后天", "每个季度", "发工资之后"])
def test_unsupported_future_transfer_survives_intent_gate(when):
    text = f"{when}给林悦转300元"
    intent = offline_intent(text, ["林悦"], [])
    assert intent.action == "transfer"
    assert intent.schedule_requested is True
    result = parse_schedule(text, NOW, requested=intent.schedule_requested)
    assert result is not None
    assert result.get("error")
    assert "execute_at" not in result
    # These same phrases are ordinary data when they occur only in the memo.
    memo_text = f"给林悦转300元，备注{when}房租"
    assert offline_intent(memo_text, ["林悦"], []).schedule_requested is False
    assert parse_schedule(memo_text, NOW) is None


def test_relative_day_is_anchored_when_first_supplied():
    pending = parse_schedule("明天转给林悦300元", NOW)
    later = datetime(2026, 10, 4, 9, 0, tzinfo=SHANGHAI)
    result = parse_schedule("20:00", later, pending)
    assert result["execute_at"] == "2026-10-04T20:00:00+08:00"


def test_time_then_date_can_fill_slots_in_either_order():
    pending = parse_schedule("晚上八点转给林悦300元", NOW)
    assert pending["time"] == "20:00"
    result = parse_schedule("明天", NOW, pending)
    assert result["execute_at"] == "2026-10-04T20:00:00+08:00"


def test_vague_time_correction_clears_previous_clock_time():
    original = parse_schedule("明天上午9点转账", NOW)
    corrected = parse_schedule("改成后天晚上", NOW, original)
    assert corrected["day"] == "2026-10-05"
    assert not corrected.get("time")
    assert "execute_at" not in corrected
    still_missing = parse_schedule("给林悦300元", NOW, corrected)
    assert "execute_at" not in still_missing
    resolved = parse_schedule("20:00", NOW, still_missing)
    assert resolved["execute_at"] == "2026-10-05T20:00:00+08:00"


def test_invalid_date_correction_does_not_restore_stale_date():
    original = parse_schedule("明天20:00转账", NOW)
    corrected = parse_schedule("10月32日", NOW, original)
    assert not corrected.get("day")
    assert "execute_at" not in parse_schedule("林悦300元", NOW, corrected)


def test_unsupported_correction_requires_new_date_and_time():
    original = parse_schedule("明天20:00转账", NOW)
    corrected = parse_schedule("改成下周", NOW, original)
    assert not corrected.get("day")
    assert not corrected.get("time")
    assert "execute_at" not in parse_schedule("林悦300元", NOW, corrected)


def test_note_is_data_and_cannot_create_a_schedule_or_transfer_fields():
    text = "转给林悦300元，备注明天20:00房租"
    assert not wants_schedule(text)
    assert parse_schedule(text, NOW) is None
    intent = offline_intent(text, ["林悦"], [])
    assert intent.note == "明天20:00房租"
    assert intent.schedule_requested is False
    no_amount = offline_intent("转给林悦，备注300元，明天20:00", ["林悦"], [])
    assert no_amount.amount_yuan is None


def test_explicit_immediate_switch_removes_old_schedule():
    pending = parse_schedule("明天转给林悦300元", NOW)
    assert parse_schedule("改为现在转账", NOW, pending, requested=True) is None


def test_past_date_is_not_rolled_into_next_year():
    result = parse_schedule("1月2日20:00转账", NOW)
    assert result["day"] == "2026-01-02"
    assert "execute_at" not in result


@pytest.mark.parametrize(("amount", "expected"), [
    ("300.123", "300.123"), ("-300", "-300"), ("+300", "+300"),
    ("-￥300", "-300"), ("￥-300", "-300"), ("- ￥ 300", "-300"),
    ("- 300", "-300"), ("−300", "-300"), ("1,000", None),
    ("1，000", None), ("1e3", None), ("1e+3", None), ("1e-3", None),
    ("1e 3", None), ("300.12", "300.12"),
])
def test_transfer_amount_preserves_entire_value_for_validation(amount, expected):
    text = f"转给林悦{amount}元"
    assert extract_amount_text(text) == expected
    assert offline_intent(text, ["林悦"], []).amount_yuan == expected
