"""Read-only, account-scoped bill questions and reproducible evidence rules."""

from __future__ import annotations

from calendar import monthrange
from collections import defaultdict
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
import re
import sqlite3
from typing import Any

from .clock import business_date
from .db import ACCOUNT_ID, USER_ID, audit, db_session
from .schedule_time import chinese_number


RULE_VERSION = "insights-1.0"
PERIOD_RE = re.compile(
    r"(?<!上)上周(?!末|[一二三四五六日天])|(?:最近|近|过去)\s*[零〇一二三四五六七八九十两\d]+\s*天|"
    r"(?<!上)上个月|这个月|本月|(?<!上)上月|今年|去年|\d{4}-\d{2}(?!\d)"
)
AMBIGUOUS_TIME_RE = re.compile(r"最近|近期|之前|以后|昨天|今天|明天|前天|去年同期|前年|明年|周|星期|季度|半年|个月内|\d{4}年|\d{1,2}月|\d{4}-\d{2}-\d{2}")
CATEGORY_ALIASES = {"吃饭": "餐饮", "饮食": "餐饮", "交通费": "交通", "住房": "居住", "会员": "数字服务"}
BASE_CATEGORIES = {"餐饮", "交通", "居住", "日用", "数字服务", "转账", "购物", "医疗", "教育", "娱乐", "旅行"}
AMOUNT_RE = re.compile(r"(超过|大于|高于|小于|低于|少于|>|<)\s*(\d+(?:\.\d+)?)\s*(?:元|块)?")


def money(cents: int) -> str:
    return f"{Decimal(cents) / 100:.2f}"


def _month_start_previous(day: date) -> date:
    return (day.replace(day=1) - timedelta(days=1)).replace(day=1)


def _period_dates(period: str, today: date) -> tuple[date, date, date, date, str]:
    period = {"这个月": "本月", "上月": "上个月"}.get(period, period)
    if period == "上周":
        current_week_start = today - timedelta(days=today.weekday())
        end = current_week_start - timedelta(days=1)
        start = end - timedelta(days=6)
        previous_end = start - timedelta(days=1)
        return start, end, previous_end - timedelta(days=6), previous_end, period
    relative_days = re.fullmatch(r"(?:最近|近|过去)\s*([零〇一二三四五六七八九十两\d]+)\s*天", period)
    if relative_days:
        days = chinese_number(relative_days[1])
        if not 1 <= days <= 90:
            raise ValueError("相对日期范围须为 1 至 90 天，请重新指定。")
        end = today
        start = end - timedelta(days=days - 1)
        previous_end = start - timedelta(days=1)
        return start, end, previous_end - timedelta(days=days - 1), previous_end, period.strip()
    if period in ("今年", "去年"):
        year = today.year - (period == "去年")
        start = date(year, 1, 1)
        end = today if period == "今年" else date(year, 12, 31)
        previous_start = date(year - 1, 1, 1)
        previous_end = date(year - 1, 12, 31)
        label = str(year)
    else:
        if period == "本月":
            start = today.replace(day=1)
        elif period == "上个月":
            start = _month_start_previous(today)
        elif re.fullmatch(r"\d{4}-\d{2}", period):
            try:
                start = date.fromisoformat(period + "-01")
            except ValueError as exc:
                raise ValueError("月份无效，请使用 YYYY-MM，例如 2026-09。") from exc
        else:
            raise ValueError("请明确期间：本月、上个月、今年、去年，或 YYYY-MM。")
        if start > today:
            raise ValueError("该期间尚未发生，请选择不晚于演示日期的月份。")
        end = min(today, date(start.year, start.month, monthrange(start.year, start.month)[1]))
        previous_start = _month_start_previous(start)
        previous_end = start - timedelta(days=1)
        label = start.strftime("%Y-%m")
    days = min((end - start).days + 1, (previous_end - previous_start).days + 1)
    return start, end, previous_start, previous_start + timedelta(days=days - 1), label


def parse_question(conn: sqlite3.Connection, text: str, default_period: str = "本月") -> dict[str, Any]:
    """Conservative finite grammar: unrecognized constraints return clarification."""
    if not text.strip():
        raise ValueError("请输入要查询的支出问题。")
    today = date.fromisoformat(business_date(conn))
    _period_dates(default_period, today)
    if AMBIGUOUS_TIME_RE.search(PERIOD_RE.sub("", text)):
        raise ValueError("这个时间范围还不能精确解析，请改为本月、上个月、今年、去年，或 YYYY-MM。")
    periods = list(dict.fromkeys(PERIOD_RE.findall(text)))
    canonical = [{"这个月": "本月", "上月": "上个月"}.get(value, value) for value in periods]
    if len(set(canonical)) > 1:
        comparable = set(canonical) == {"本月", "上个月"} or set(canonical) == {"今年", "去年"}
        if not comparable or not re.search(r"比|变化|增加|减少|差", text):
            raise ValueError("请一次指定一个查询期间；比较可写“本月比上个月”或“今年比去年”。")
        period = "本月" if "本月" in canonical else "今年"
    else:
        period = canonical[0] if canonical else default_period
    _period_dates(period, today)
    merchants = [row[0] for row in conn.execute(
        "SELECT DISTINCT counterparty FROM transactions WHERE account_id = ? AND direction = 'out'",
        (ACCOUNT_ID,),
    )]
    merchant_matches = [name for name in merchants if name and name in text]
    # A longer literal merchant name subsumes a contained shorter one.
    merchant_matches = [name for name in merchant_matches if not any(name != other and name in other for other in merchant_matches)]
    if len(merchant_matches) > 1:
        raise ValueError("请一次指定一个商户，或去掉商户条件查询全部支出。")
    merchant = merchant_matches[0] if merchant_matches else None
    remaining = text.replace(merchant, "") if merchant else text
    categories = BASE_CATEGORIES | {row[0] for row in conn.execute(
        "SELECT DISTINCT category FROM transactions WHERE account_id = ? AND direction = 'out'", (ACCOUNT_ID,),
    )}
    categories |= {row[0] for row in conn.execute('SELECT DISTINCT category FROM bill_category_overrides WHERE account_id=?', (ACCOUNT_ID,))}
    matched_categories = {value for value in categories if value in remaining}
    matched_categories |= {category for alias, category in CATEGORY_ALIASES.items() if alias in remaining}
    if len(matched_categories) > 1:
        raise ValueError("请一次指定一个消费分类，或去掉分类条件查询全部支出。")
    category = next(iter(matched_categories), None)
    lower: int | None = None
    upper: int | None = None
    amount_matches = list(AMOUNT_RE.finditer(remaining))
    for match in amount_matches:
        try:
            value = Decimal(match.group(2))
        except InvalidOperation as exc:
            raise ValueError("金额条件无效，请输入精确到分的非负金额。") from exc
        if value.as_tuple().exponent < -2 or value > 100_000_000:
            raise ValueError("金额条件须精确到分，且不超过一亿元。")
        cents = int(value * 100)
        if match.group(1) in ("超过", "大于", "高于", ">"):
            lower = max(lower, cents) if lower is not None else cents
        else:
            upper = min(upper, cents) if upper is not None else cents
    if lower is not None and upper is not None and lower >= upper:
        raise ValueError("金额上下限没有交集，请重新指定。")
    remaining = AMOUNT_RE.sub("", remaining)
    remaining = PERIOD_RE.sub("", remaining)
    for value in sorted(categories | set(CATEGORY_ALIASES), key=len, reverse=True):
        remaining = remaining.replace(value, "")
    # Only supported query/explanation words may remain; instructions, unknown
    # merchants and unsupported filters are not silently widened to all spending.
    phrases = ("帮我", "请问", "请", "查一下", "查询", "查找", "找出", "看看", "查看", "分析", "统计", "汇总", "账单", "支出", "消费", "花费", "花了", "花得", "花的", "花", "多少钱", "多少", "为什么", "为何", "比", "增加", "减少", "多了", "少了", "变化", "差额", "异常", "重复", "扣费", "交易", "记录", "明细", "金额", "商户", "分类", "哪些", "有什么", "有没有", "和", "与", "的", "在", "共", "总计", "一共", "总额", "多", "少", "是否", "需要", "值得", "检查", "复核", "元", "块")
    for phrase in sorted(phrases, key=len, reverse=True):
        remaining = remaining.replace(phrase, "")
    if re.sub(r"[\s，。！？、,:：;；?!.（）()\"'“”]", "", remaining):
        raise ValueError("还有未识别的查询条件。请使用“上个月餐饮花了多少”或“本月城市咖啡超过 50 元的消费”等表达；只支持支出查询。")
    return {"period": period, "category": category, "merchant": merchant,
            "min_amount_cents": lower, "max_amount_cents": upper}


def _read_transactions(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    from .bill_preferences import apply_effective_categories
    return apply_effective_categories(conn, [{**dict(row), "amount_yuan": money(row["amount_cents"])} for row in conn.execute(
        "SELECT id, posted_on, direction, amount_cents, counterparty, category, note FROM transactions "
        "WHERE account_id = ? AND direction = 'out' ORDER BY posted_on DESC, id", (ACCOUNT_ID,),
    )])


def _matches(tx: dict[str, Any], filters: dict[str, Any]) -> bool:
    return ((filters.get("category") is None or tx["category"] == filters["category"])
            and (filters.get("merchant") is None or tx["counterparty"] == filters["merchant"])
            and (filters.get("min_amount_cents") is None or tx["amount_cents"] > filters["min_amount_cents"])
            and (filters.get("max_amount_cents") is None or tx["amount_cents"] < filters["max_amount_cents"]))


def _changes(current: list[dict], previous: list[dict], field: str) -> list[dict]:
    names = {tx[field] for tx in current + previous}
    changes = []
    for name in names:
        current_rows = [tx for tx in current if tx[field] == name]
        previous_rows = [tx for tx in previous if tx[field] == name]
        now = sum(tx["amount_cents"] for tx in current_rows)
        before = sum(tx["amount_cents"] for tx in previous_rows)
        changes.append({"name": name, "current_yuan": money(now), "previous_yuan": money(before),
                        "delta_yuan": money(now - before),
                        "transaction_ids": [tx["id"] for tx in current_rows + previous_rows]})
    return sorted(changes, key=lambda item: (-abs(Decimal(item["delta_yuan"])), item["name"]))


def _alerts(conn: sqlite3.Connection, selected: list[dict], history: list[dict]) -> tuple[list[dict], list[str]]:
    alerts: list[dict] = []
    limitations: list[str] = []
    duplicates: dict[tuple, list[dict]] = defaultdict(list)
    for tx in selected:
        duplicates[(tx["posted_on"], tx["counterparty"], tx["amount_cents"])].append(tx)
    for items in duplicates.values():
        if len(items) > 1:
            alerts.append({"id": "duplicate-" + items[0]["id"], "type": "possible_duplicate",
                           "title": f"{items[0]['counterparty']} 有同日同额交易",
                           "reason": f"同一商户、同一天、同为 ¥{items[0]['amount_yuan']}，共 {len(items)} 笔。可能是正常多次消费，请逐笔复核。",
                           "evidence": items, "related_subscription_id": None})
    subscriptions = {row["merchant"]: dict(row) for row in conn.execute(
        "SELECT id, merchant, status FROM subscriptions WHERE user_id = ?", (USER_ID,),
    )}
    insufficient = set()
    price_keys = set()
    # Baseline always predates the candidate, never includes the suspicious
    # transaction itself. A later ordinary payment must not hide an earlier spike.
    for tx in selected:
        merchant = tx["counterparty"]
        baseline = [item for item in history if item["counterparty"] == merchant and item["posted_on"] < tx["posted_on"]][:12]
        if len(baseline) < 3:
            insufficient.add(merchant)
            continue
        values = sorted(item["amount_cents"] for item in baseline)
        middle = len(values) // 2
        # Doubled median avoids any binary floating point or rounding decisions.
        median_twice = values[middle] * 2 if len(values) % 2 else values[middle - 1] + values[middle]
        if tx["amount_cents"] * 2 > median_twice * 2:
            alerts.append({"id": "spike-" + tx["id"], "type": "amount_spike", "title": f"{merchant} 本次金额高于历史样本",
                           "reason": f"本次金额超过此前 {len(baseline)} 笔历史记录中位数的 2 倍；规则仅生成复核线索。",
                           "evidence": [tx] + baseline, "related_subscription_id": subscriptions.get(merchant, {}).get("id")})
        # Price change requires a known agreement and at least three earlier
        # month samples. Regular shopping alone must not become a subscription.
        months: dict[str, dict] = {}
        for item in baseline:
            if item["posted_on"][:7] != tx["posted_on"][:7]:
                months.setdefault(item["posted_on"][:7], item)
        monthly = list(months.values())[:6]
        if merchant in subscriptions and len(monthly) >= 3:
            preceding = monthly[0]
            price_key = (merchant, tx["posted_on"][:7], tx["amount_cents"], preceding["amount_cents"])
            if tx["amount_cents"] > preceding["amount_cents"] and price_key not in price_keys:
                price_keys.add(price_key)
                alerts.append({"id": "price-" + tx["id"], "type": "subscription_price_increase",
                               "title": f"{merchant} 扣费金额增加",
                               "reason": f"已有模拟代扣协议；本次 ¥{tx['amount_yuan']}，上个有记录月份 ¥{preceding['amount_yuan']}，增加 ¥{money(tx['amount_cents'] - preceding['amount_cents'])}。账单不能证明商户调整了标价，请核对协议。",
                               "evidence": [tx] + monthly, "related_subscription_id": subscriptions[merchant]["id"]})
        elif merchant in subscriptions:
            insufficient.add(merchant)
    if insufficient:
        limitations.append("以下商户历史样本不足（金额突增至少需 3 笔历史记录，订阅金额变化至少需 3 个历史月份）：" + "、".join(sorted(insufficient)) + "。")
    return alerts, limitations


def build_report(conn: sqlite3.Connection, filters: dict[str, Any]) -> dict[str, Any]:
    today = date.fromisoformat(business_date(conn))
    start, end, previous_start, previous_end, label = _period_dates(filters.get("period", "本月"), today)
    days = (previous_end - previous_start).days + 1
    current_comparison_end = start + timedelta(days=days - 1)
    history = [tx for tx in _read_transactions(conn) if tx["posted_on"] <= today.isoformat()]
    filtered = [tx for tx in history if _matches(tx, filters)]
    selected = [tx for tx in filtered if start.isoformat() <= tx["posted_on"] <= end.isoformat()]
    current_comparison = [tx for tx in selected if tx["posted_on"] <= current_comparison_end.isoformat()]
    previous = [tx for tx in filtered if previous_start.isoformat() <= tx["posted_on"] <= previous_end.isoformat()]
    now = sum(tx["amount_cents"] for tx in current_comparison)
    before = sum(tx["amount_cents"] for tx in previous)
    total = sum(tx["amount_cents"] for tx in selected)
    percent = str((Decimal(now - before) * 100 / before).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)) if before else None
    alerts, limitations = _alerts(conn, selected, history)
    limitations.insert(0, "统计来自已保存的模拟流水；有交易的日期不代表期间记录完整，未出现交易不等于没有消费。")
    if not previous:
        limitations.append("比较基期没有匹配交易，无法判断增长原因或计算增长百分比。")
    if current_comparison_end != end:
        limitations.append(f"为对齐天数，变化比较仅使用当期前 {days} 天；上方总额仍覆盖整个查询期间。")
    category_changes = _changes(current_comparison, previous, "category")
    merchant_changes = _changes(current_comparison, previous, "counterparty")
    summary = f"{start.isoformat()} 至 {end.isoformat()}，匹配支出 {len(selected)} 笔，共 ¥{money(total)}。"
    if selected and previous:
        word = "增加" if now > before else "减少" if now < before else "持平"
        summary += f"可比的 {days} 天内，支出{word}" + (f" ¥{money(abs(now - before))}。" if now != before else "。")
        main = next((item for item in category_changes if Decimal(item["delta_yuan"]) != 0), None)
        if main:
            delta = Decimal(main["delta_yuan"])
            summary += f"金额变化最大的分类是{main['name']}，{'增加' if delta > 0 else '减少'} ¥{abs(delta):.2f}；这说明金额贡献，不推断消费动机。"
    elif not selected:
        summary += "该条件下没有已保存的支出记录。"
    dates = [tx["posted_on"] for tx in selected]
    return {"rule_version": RULE_VERSION, "period": label, "start_date": start.isoformat(), "end_date": end.isoformat(),
            "filters": {"category": filters.get("category"), "merchant": filters.get("merchant"),
                        "min_amount_yuan": money(filters["min_amount_cents"]) if filters.get("min_amount_cents") is not None else None,
                        "max_amount_yuan": money(filters["max_amount_cents"]) if filters.get("max_amount_cents") is not None else None},
            "total_yuan": money(total), "transaction_count": len(selected), "transactions": selected,
            "coverage": {"first_date": min(dates) if dates else None, "last_date": max(dates) if dates else None,
                         "months_with_data": len({value[:7] for value in dates}), "complete_history": False},
            "comparison": {"current_start": start.isoformat(), "current_end": current_comparison_end.isoformat(),
                           "previous_start": previous_start.isoformat(), "previous_end": previous_end.isoformat(), "equal_days": days,
                           "current_total_yuan": money(now), "previous_total_yuan": money(before), "delta_yuan": money(now - before),
                           "delta_percent": percent, "category_changes": category_changes, "merchant_changes": merchant_changes,
                           "transactions": current_comparison + previous},
            "alerts": alerts, "limitations": limitations, "summary": summary}


def get_insights(period: str = "本月") -> dict[str, Any]:
    with db_session() as conn:
        try:
            report = build_report(conn, {"period": period})
        except ValueError as exc:
            return {"status": "needs_clarification", "mode": "offline", "message": str(exc), "clarification": str(exc)}
        return {"status": "ok", "mode": "offline", "message": report["summary"], "report": report}


def query_insights(session_id: str, message: str, period: str = "本月") -> dict[str, Any]:
    with db_session() as conn:
        try:
            filters = parse_question(conn, message, period)
            report = build_report(conn, filters)
        except ValueError as exc:
            return {"status": "needs_clarification", "mode": "offline", "message": str(exc), "clarification": str(exc)}
        audit(conn, session_id, "insights_queried", {"period": report["period"], "filters": report["filters"], "rule_version": RULE_VERSION})
        return {"status": "ok", "mode": "offline", "message": report["summary"], "report": report}
