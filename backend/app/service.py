"""Deterministic banking tools and the confirmation boundary."""

from __future__ import annotations

import json
import hashlib
import re
import secrets
import os
import sqlite3
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from typing import Any

from fastapi import HTTPException

from .agent import Intent, explicitly_declines_transfer, extract_amount_text, parse_intent
from .db import ACCOUNT_ID, USER_ID, audit, connect, db_session, utc_now
from .clock import business_date, business_now
from .schedule_time import instruction_text, parse_schedule
from .execution_controls import available_cents, debit, require_verified


def money(cents: int) -> str:
    return f"{Decimal(cents) / 100:.2f}"


def cents_from_yuan(value: str | float | None) -> int | None:
    if value is None:
        return None
    try:
        amount = Decimal(str(value))
    except InvalidOperation:
        return None
    if not amount.is_finite() or amount <= 0 or amount.as_tuple().exponent < -2:
        return None
    cents = int(amount * 100)
    return cents if 0 < cents <= 10_000_000 else None


def mask_phone(phone: str) -> str:
    return f"{phone[:3]}****{phone[-4:]}"


def contact_fingerprint(contact: sqlite3.Row) -> str:
    return hashlib.sha256(f"{contact['name']}|{contact['phone']}|{contact['account_ref']}".encode()).hexdigest()


def contact_options() -> list[dict[str, str]]:
    with db_session() as conn:
        rows = conn.execute(
            "SELECT id, name, phone FROM contacts WHERE user_id = ? AND verified = 1 ORDER BY name, id",
            (USER_ID,),
        ).fetchall()
        return [{"id": row["id"], "name": row["name"], "phone_masked": mask_phone(row["phone"])} for row in rows]


def overview() -> dict[str, Any]:
    with db_session() as conn:
        account = conn.execute("SELECT * FROM accounts WHERE id = ?", (ACCOUNT_ID,)).fetchone()
        transactions = conn.execute(
            "SELECT id, posted_on, direction, amount_cents, counterparty, category, note "
            "FROM transactions WHERE account_id = ? ORDER BY posted_on DESC, id DESC LIMIT 8",
            (ACCOUNT_ID,),
        ).fetchall()
        subscriptions = conn.execute(
            "SELECT id, merchant, amount_cents, renewal_on, status FROM subscriptions "
            "WHERE user_id = ? ORDER BY merchant",
            (USER_ID,),
        ).fetchall()
        recent_audit = conn.execute(
            "SELECT at, event, details_json FROM audit ORDER BY id DESC LIMIT 8"
        ).fetchall()
        return {
            "demo_date": business_date(conn),
            "demo_now": business_now(conn).isoformat(timespec="seconds"),
            "account": {"label": account["label"], "balance_yuan": money(account["balance_cents"]),
                        "available_yuan": money(available_cents(conn)),
                        "reserved_yuan": money(account["balance_cents"] - available_cents(conn))},
            "transactions": [
                {**dict(row), "amount_yuan": money(row["amount_cents"])} for row in transactions
            ],
            "subscriptions": [
                {**dict(row), "amount_yuan": money(row["amount_cents"])} for row in subscriptions
            ],
            "subscription_signals": subscription_signals(conn),
            "audit": [
                {"at": row["at"], "event": row["event"], "details": json.loads(row["details_json"])}
                for row in recent_audit
            ],
        }


def get_context(conn: sqlite3.Connection, session_id: str) -> dict[str, Any]:
    row = conn.execute("SELECT context_json FROM sessions WHERE id = ?", (session_id,)).fetchone()
    return json.loads(row["context_json"]) if row else {}


def set_context(conn: sqlite3.Connection, session_id: str, context: dict[str, Any]) -> None:
    conn.execute(
        "INSERT INTO sessions (id, context_json, updated_at) VALUES (?, ?, ?) "
        "ON CONFLICT(id) DO UPDATE SET context_json = excluded.context_json, updated_at = excluded.updated_at",
        (session_id, json.dumps(context, ensure_ascii=False), utc_now()),
    )


def reply(message: str, session_id: str, mode: str, **extra: Any) -> dict[str, Any]:
    return {"session_id": session_id, "message": message, "mode": mode, **extra}


def decline_transfer_request(session_id: str) -> dict[str, Any]:
    """Withdraw this session's unconfirmed transfer draft/actions atomically."""
    with db_session(immediate=True) as conn:
        context = get_context(conn, session_id)
        had_draft = context.pop("pending_transfer", None) is not None
        pending = conn.execute(
            "SELECT id FROM actions WHERE session_id=? AND type='transfer' AND status='pending'",
            (session_id,),
        ).fetchall()
        action_ids = [row["id"] for row in pending]
        for action_id in action_ids:
            conn.execute(
                "UPDATE actions SET status='failed', result_json=? WHERE id=? AND status='pending'",
                (json.dumps({"status": "cancelled", "message": "用户明确撤回了转账请求，未执行"}, ensure_ascii=False), action_id),
            )
            conn.execute(
                "UPDATE action_challenges SET status='revoked' WHERE action_id=? AND status IN ('active','verified')",
                (action_id,),
            )
        set_context(conn, session_id, context)
        audit(conn, session_id, "transfer_intent_withdrawn", {
            "pending_draft_cleared": had_draft,
            "invalidated_action_ids": action_ids,
        })
    return reply(
        "收到，你明确表示不进行这笔转账。未确认的转账草稿与待确认操作已清除，未发生扣款。若要取消已确认的预约，请到“我的预约”中选择该笔取消。",
        session_id, "offline",
    )


async def process_message(session_id: str, message: str) -> dict[str, Any]:
    request_text = instruction_text(message)
    if explicitly_declines_transfer(request_text):
        return decline_transfer_request(session_id)
    if re.search(r'少花|省下|节省|省钱|减少.{0,6}支出', request_text):
        from .plans import preview_plan
        plan_reply = preview_plan(session_id, request_text)
        return reply(plan_reply['message'], session_id, 'offline', workflow={'view':'tasks','section':'plans','message':request_text,'plan_id':plan_reply.get('plan',{}).get('id')})
    workflow = None
    if '生日' in request_text:
        workflow = ('tasks', 'life', '生日计划会先核对日期、预算、收货人和商品，再授权预留及模拟下单。')
    elif re.search(r'锁卡|解锁|挂失|卡找不到|卡片|银行卡|申请.{0,4}卡|提额', request_text):
        workflow = ('cards', None, '请在卡片页核对具体卡号尾号和操作影响；当前需求已带入。')
    elif re.search(r'理财|申购|赎回|风险测评|产品对比|有笔钱暂时不用', request_text):
        workflow = ('investments', None, '理财助手会核对测评、期限、风险与流动性条件；当前需求已带入，不会自动买入。')
    if workflow:
        with db_session() as conn:
            set_context(conn,session_id,{})
            audit(conn,session_id,'workflow_routed',{'view':workflow[0],'section':workflow[1]})
        return reply(workflow[2],session_id,'offline',workflow={'view':workflow[0],'section':workflow[1],'message':request_text})
    if request_text.strip(" ，。！!？?") in ("取消", "算了", "不转了", "取消预约", "取消转账", "取消这笔", "取消这笔预约"):
        with db_session() as conn:
            set_context(conn, session_id, {})
        return reply("已清除未完成的输入。已确认的预约请在“我的预约”中选择并取消。", session_id, "offline")
    with db_session() as conn:
        contacts = conn.execute("SELECT name FROM contacts WHERE user_id = ?", (USER_ID,)).fetchall()
        subscriptions = conn.execute(
            "SELECT merchant FROM subscriptions WHERE user_id = ? AND status = 'active'", (USER_ID,)
        ).fetchall()
        context = get_context(conn, session_id)
        now = business_now(conn)
    contact_names = list(dict.fromkeys(row["name"] for row in contacts))
    subscription_names = [row["merchant"] for row in subscriptions]
    parsed = await parse_intent(message, contact_names, subscription_names)
    intent = parsed.intent
    if intent.action in ('transfer', 'unknown'):
        from .aliases import resolve_transfer_alias
        with db_session() as conn:
            resolved = resolve_transfer_alias(conn, session_id, message)
        if resolved:
            if resolved['status'] != 'resolved':
                return reply('称呼对应的联系人需要重新核对，请在智能转账页查看已保存别名。', session_id, 'offline')
            if intent.action == 'transfer' or context.get('pending_transfer'):
                intent = intent.model_copy(update={'action':'transfer', 'recipient':resolved['recipient'], 'phone':resolved['phone']})
    pending = context.get("pending_transfer") or {}
    schedule = None
    if context.get("pending_transfer") and intent.action in ("unknown", "transfer"):
        pending = context["pending_transfer"]
        supplied_amount = intent.amount_yuan if intent.amount_yuan is not None else extract_amount(request_text)
        # An explicit but invalid correction must not silently restore an old amount.
        amount = supplied_amount if supplied_amount is not None or re.search(r"元|块", request_text) else pending.get("amount_yuan")
        intent = Intent(
            action="transfer",
            recipient=intent.recipient or next((name for name in contact_names if name in request_text), pending.get("recipient")),
            phone=intent.phone or next((row["phone"] for row in _matching_phone(request_text)), pending.get("phone")),
            amount_yuan=amount,
            note=intent.note or pending.get("note"),
            schedule_requested=intent.schedule_requested or pending.get("schedule_requested", False),
        )
    if intent.action == "transfer":
        schedule = parse_schedule(message, now, pending.get("schedule"), intent.schedule_requested)
    if context.get("pending_cancel") and intent.action == "unknown":
        matched = next((name for name in subscription_names if name in message), None)
        if matched:
            intent = Intent(action="subscription_cancel", subscription=matched)

    with db_session() as conn:
        audit(conn, session_id, "intent_parsed", {"action": intent.action, "mode": parsed.mode, "model_call": parsed.metadata})
        if parsed.usage:
            conn.execute(
                "INSERT INTO model_usage (at, model, prompt_tokens, completion_tokens) VALUES (?, ?, ?, ?)",
                (utc_now(), os.getenv('DEEPSEEK_MODEL', 'deepseek-flash'), parsed.usage["prompt_tokens"], parsed.usage["completion_tokens"]),
            )
        if intent.action == "aa_split" or (context.get("pending_aa") and intent.action == "unknown"):
            from .aa import interpret_message
            return interpret_message(conn, session_id, message, parsed.mode, intent)
        if intent.action == "balance_query":
            account = conn.execute("SELECT balance_cents FROM accounts WHERE id = ?", (ACCOUNT_ID,)).fetchone()
            set_context(conn, session_id, {})
            return reply(f"日常账户余额为 ¥{money(account['balance_cents'])}，可用余额 ¥{money(available_cents(conn))}，预留 ¥{money(account['balance_cents'] - available_cents(conn))}。数据来自模拟账户。", session_id, parsed.mode)
        if intent.action == "bill_summary":
            set_context(conn, session_id, {})
            if re.search(r"餐饮|交通|日用|居住|超过|低于|小于|为什么|变化|增加|减少|异常|重复|上月|商户", request_text):
                from .insights import build_report, parse_question
                try:
                    insight = build_report(conn, parse_question(conn, request_text))
                    return reply(insight['summary'], session_id, 'offline', insight_query=request_text)
                except ValueError as exc:
                    return reply(str(exc), session_id, 'offline', insight_query=request_text)
            return bill_summary(conn, session_id, parsed.mode, intent.period or message)
        if intent.action == "subscription_list":
            set_context(conn, session_id, {})
            active = conn.execute(
                "SELECT id, merchant, amount_cents, renewal_on FROM subscriptions "
                "WHERE user_id = ? AND status = 'active' ORDER BY renewal_on",
                (USER_ID,),
            ).fetchall()
            if not active:
                return reply("当前没有正在生效的模拟代扣协议。", session_id, parsed.mode, subscriptions=[])
            signals = {row["merchant"]: row for row in subscription_signals(conn)}
            lines = []
            for row in active:
                suffix = "；近两个月账单均有扣费记录" if row["merchant"] in signals else ""
                lines.append(f"{row['merchant']}：¥{money(row['amount_cents'])}，下次扣费 {row['renewal_on']}{suffix}")
            return reply("发现以下模拟订阅：\n" + "\n".join(lines), session_id, parsed.mode,
                         subscriptions=[dict(row) for row in active], subscription_signals=list(signals.values()))
        if intent.action == "subscription_cancel":
            return prepare_cancel(conn, session_id, parsed.mode, intent, message)
        if intent.action == "transfer":
            return prepare_transfer(conn, session_id, parsed.mode, intent, message, schedule)
        set_context(conn, session_id, {})
        return reply("我目前能处理余额查询、账单分析、转账、AA 分摊和订阅管理。请描述其中一项具体需求。", session_id, parsed.mode)


def _matching_phone(text: str) -> list[dict[str, str]]:
    return [{"phone": match} for match in re.findall(r"(?<!\d)1[3-9]\d{9}(?!\d)", text)]


def extract_amount(text: str) -> str | None:
    return extract_amount_text(text)


def subscription_signals(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """Find recurring debit evidence without trusting merchant prose as instructions."""
    demo_day = date.fromisoformat(business_date(conn))
    current = demo_day.replace(day=1)
    previous = (current - timedelta(days=1)).replace(day=1)
    rows = conn.execute(
        "SELECT id, posted_on, counterparty, amount_cents FROM transactions "
        "WHERE account_id = ? AND direction = 'out' AND category = '数字服务' "
        "AND posted_on >= ? AND posted_on <= ? ORDER BY posted_on DESC",
        (ACCOUNT_ID, previous.isoformat(), demo_day.isoformat()),
    ).fetchall()
    by_merchant: dict[str, dict[str, list[sqlite3.Row]]] = {}
    for row in rows:
        by_merchant.setdefault(row["counterparty"], {}).setdefault(row["posted_on"][:7], []).append(row)
    active = {
        row["merchant"]: row
        for row in conn.execute("SELECT merchant, renewal_on FROM subscriptions WHERE user_id = ? AND status = 'active'", (USER_ID,))
    }
    signals = []
    for merchant, months in by_merchant.items():
        if current.strftime("%Y-%m") not in months or previous.strftime("%Y-%m") not in months:
            continue
        last = months[current.strftime("%Y-%m")][0]
        sub = active.get(merchant)
        renewal = sub["renewal_on"] if sub else None
        days_until = (date.fromisoformat(renewal) - demo_day).days if renewal else None
        signals.append({
            "merchant": merchant,
            "evidence_ids": [row["id"] for group in months.values() for row in group],
            "last_amount_yuan": money(last["amount_cents"]),
            "renewal_on": renewal,
            "days_until_renewal": days_until,
            "reminder": days_until is not None and 0 <= days_until <= 30,
        })
    return sorted(signals, key=lambda item: item["renewal_on"] or "9999-12-31")


def bill_summary(conn: sqlite3.Connection, session_id: str, mode: str, period: str) -> dict[str, Any]:
    demo_day = date.fromisoformat(business_date(conn))
    annual = any(value in period for value in ("今年", "去年", "全年", "年度", "年账单"))
    if "去年" in period:
        first = date(demo_day.year - 1, 1, 1)
        end = date(demo_day.year, 1, 1)
    elif annual:
        first = date(demo_day.year, 1, 1)
        end = demo_day + timedelta(days=1)
    elif "上个月" in period:
        first = (demo_day.replace(day=1) - timedelta(days=1)).replace(day=1)
    else:
        first = demo_day.replace(day=1)
    if not annual:
        end = date(first.year + (first.month == 12), first.month % 12 + 1, 1)
    rows = conn.execute(
        "SELECT id, posted_on, amount_cents, category, counterparty, note FROM transactions "
        "WHERE account_id = ? AND direction = 'out' AND posted_on >= ? AND posted_on < ? "
        "ORDER BY posted_on DESC",
        (ACCOUNT_ID, first.isoformat(), end.isoformat()),
    ).fetchall()
    categories: dict[str, int] = {}
    from .bill_preferences import apply_effective_categories
    rows = apply_effective_categories(conn, rows)
    for row in rows:
        categories[row["category"]] = categories.get(row["category"], 0) + row["amount_cents"]
    total = sum(categories.values())
    month_name = f"{first.year} 年" if annual else f"{first.year} 年 {first.month} 月"
    amounts = sorted(row["amount_cents"] for row in rows)
    median = amounts[len(amounts) // 2] if amounts else 0
    threshold = max(50_000, median * 3)
    alerts = [
        {"transaction_id": row["id"], "counterparty": row["counterparty"], "amount_yuan": money(row["amount_cents"]),
         "reason": f"金额超过 ¥{money(threshold)} 的透明规则阈值，建议复核"}
        for row in rows if row["amount_cents"] > threshold
    ]
    category_lines = "、".join(f"{name} ¥{money(cents)}" for name, cents in sorted(categories.items(), key=lambda item: -item[1]))
    message = f"{month_name}模拟账户支出共 ¥{money(total)}，共 {len(rows)} 笔。" + (
        f"分类：{category_lines}。" if rows else "没有支出记录。"
    )
    if alerts:
        message += f"另有 {len(alerts)} 笔高金额交易建议人工复核；这不是欺诈判定。"
    return reply(
        message, session_id, mode,
        report={
            "period": first.strftime("%Y" if annual else "%Y-%m"),
            "total_yuan": money(total),
            "categories": [{"name": name, "amount_yuan": money(cents)} for name, cents in sorted(categories.items(), key=lambda item: -item[1])],
            "transaction_ids": [row["id"] for row in rows],
            "transactions": [
                {"id": row["id"], "posted_on": row["posted_on"], "category": row["category"],
                 "counterparty": row["counterparty"], "amount_yuan": money(row["amount_cents"]),
                 "original_category": row.get('original_category', row['category']),
                 "classification_reason": row.get('classification_reason'),
                 "classification_version": row.get('classification_version'),
                 "note": row["note"]}
                for row in rows
            ],
            "alerts": alerts,
        },
    )


def direct_bill_report(period: str) -> dict[str, Any]:
    with db_session() as conn:
        return bill_summary(conn, "direct-ui", "offline", period)["report"]


def direct_prepare_transfer(session_id: str, contact_id: str, amount_yuan: str, note: str) -> dict[str, Any]:
    with db_session() as conn:
        contact = conn.execute(
            "SELECT name, phone FROM contacts WHERE id = ? AND user_id = ? AND verified = 1",
            (contact_id, USER_ID),
        ).fetchone()
        if contact is None:
            raise HTTPException(status_code=404, detail="未找到已验证收款人")
        if cents_from_yuan(amount_yuan) is None:
            raise HTTPException(status_code=422, detail="请输入大于零且精确到分的有效金额")
        intent = Intent(action="transfer", phone=contact["phone"], amount_yuan=amount_yuan, note=note)
        return prepare_transfer(conn, session_id, "offline", intent, "direct-ui")


def resolve_transfer_contact(session_id: str, contact_id: str) -> dict[str, Any]:
    with db_session() as conn:
        pending = get_context(conn, session_id).get("pending_transfer")
        if not isinstance(pending, dict) or not pending.get("recipient"):
            raise HTTPException(status_code=409, detail="没有等待选择收款人的转账，请重新描述需求")
        contact = conn.execute(
            "SELECT name, phone FROM contacts WHERE id = ? AND user_id = ? AND verified = 1",
            (contact_id, USER_ID),
        ).fetchone()
        if contact is None or contact["name"] != pending["recipient"]:
            raise HTTPException(status_code=404, detail="该收款人不在当前候选列表中")
        intent = Intent(
            action="transfer", recipient=contact["name"], phone=contact["phone"],
            amount_yuan=pending.get("amount_yuan"), note=pending.get("note"),
            schedule_requested=pending.get("schedule_requested", False),
        )
        return prepare_transfer(conn, session_id, "offline", intent, "contact-choice", pending.get("schedule"))


def direct_prepare_cancel(session_id: str, subscription_id: str) -> dict[str, Any]:
    with db_session() as conn:
        sub = conn.execute(
            "SELECT * FROM subscriptions WHERE id = ? AND user_id = ? AND status = 'active'",
            (subscription_id, USER_ID),
        ).fetchone()
        if sub is None:
            raise HTTPException(status_code=404, detail="未找到生效中的代扣协议")
        payload = {
            "subscription_id": sub["id"], "merchant": sub["merchant"],
            "amount_yuan": money(sub["amount_cents"]), "renewal_on": sub["renewal_on"],
        }
        action = create_action(conn, session_id, "subscription_cancel", "yellow", payload)
        return reply("请确认取消这项模拟代扣协议。确认后将不再生成后续自动扣费。",
                     session_id, "offline", pending_action=action)


def prepare_transfer(
    conn: sqlite3.Connection, session_id: str, mode: str, intent: Intent, raw_message: str,
    schedule: dict | None = None,
) -> dict[str, Any]:
    pending_data = {**intent.model_dump(), "schedule": schedule, "schedule_requested": schedule is not None}
    amount = cents_from_yuan(intent.amount_yuan)
    if amount is None:
        set_context(conn, session_id, {"pending_transfer": pending_data})
        return reply("请告诉我转账金额，精确到分，例如“转给林悦 300 元”。", session_id, mode)
    candidates: list[sqlite3.Row] = []
    if intent.phone:
        candidates = conn.execute(
            "SELECT * FROM contacts WHERE user_id = ? AND phone = ? AND verified = 1",
            (USER_ID, intent.phone),
        ).fetchall()
    else:
        name = intent.recipient or next(
            (row["name"] for row in conn.execute("SELECT name FROM contacts WHERE user_id = ?", (USER_ID,)) if row["name"] in instruction_text(raw_message)),
            None,
        )
        if name:
            candidates = conn.execute(
                "SELECT * FROM contacts WHERE user_id = ? AND name = ? AND verified = 1",
                (USER_ID, name),
            ).fetchall()
    if not candidates:
        set_context(conn, session_id, {"pending_transfer": pending_data})
        return reply("未找到已验证的收款人。请提供已保存联系人姓名或完整手机号。", session_id, mode)
    if len(candidates) > 1:
        pending = pending_data
        pending["recipient"] = candidates[0]["name"]
        set_context(conn, session_id, {"pending_transfer": pending})
        choices = [{"id": row["id"], "name": row["name"], "phone": mask_phone(row["phone"])} for row in candidates]
        return reply("找到多位同名收款人，请选择对应联系人，或补充完整手机号后继续。", session_id, mode, choices=choices)
    contact = candidates[0]
    if schedule is not None:
        # Preserve resolved contact even while the user supplies date/time next.
        pending_data.update(recipient=contact["name"], phone=contact["phone"])
        if schedule.get("error") or not schedule.get("execute_at"):
            set_context(conn, session_id, {"pending_transfer": pending_data})
            return reply(schedule.get("error") or "请补充预约日期和具体时间。", session_id, mode)
        if datetime.fromisoformat(schedule["execute_at"]) <= business_now(conn):
            schedule = {**schedule, "error": "预约时间已过去，请重新提供日期和时间。"}
            pending_data["schedule"] = schedule
            set_context(conn, session_id, {"pending_transfer": pending_data})
            return reply(schedule["error"], session_id, mode)
    if schedule is None and amount > available_cents(conn):
        set_context(conn, session_id, {})
        return reply("可用余额不足，未创建转账；已预留资金不能用于其他支出。", session_id, mode)
    sent_today = conn.execute(
        "SELECT COALESCE(SUM(amount_cents), 0) FROM transactions "
        "WHERE account_id = ? AND posted_on = ? AND category IN ('转账','AA结算') AND direction = 'out'",
        (ACCOUNT_ID, business_date(conn)),
    ).fetchone()[0]
    tier = "red" if (amount if schedule is not None else sent_today + amount) > 100_000 else "yellow"
    payload = {
        "contact_id": contact["id"],
        "recipient": contact["name"],
        "phone_masked": mask_phone(contact["phone"]),
        "amount_cents": amount,
        "amount_yuan": money(amount),
        "note": (intent.note or "转账")[:100],
        "contact_fingerprint": contact_fingerprint(contact),
    }
    from .aliases import similar_transfers
    similar = similar_transfers(conn, contact['id'], amount)
    if similar:
        payload['similar_transfers'] = similar
    if schedule is not None:
        payload.update(execute_at=schedule["execute_at"], timezone="Asia/Shanghai",
                       window_expires_at=(datetime.fromisoformat(schedule["execute_at"]) + timedelta(minutes=10)).isoformat(timespec="seconds"))
    action = create_action(conn, session_id, "scheduled_transfer" if schedule is not None else "transfer", tier, payload)
    set_context(conn, session_id, {})
    if tier == "red":
        return reply("转账计划已生成。日累计金额超过 ¥1000，需独立模拟强验证。" +
                     ("当前预约仅支持单笔不超过 ¥1000。" if schedule else "验证并确认后才会执行。"), session_id, mode, pending_action=action)
    if schedule is not None:
        return reply("请核对预约日期、收款人和金额。确认后到期自动检查并执行，不会提前冻结余额；超过执行时间 10 分钟则失效。",
                     session_id, mode, pending_action=action)
    return reply("请核对收款人、金额和备注。确认后才会从模拟账户扣款。", session_id, mode, pending_action=action)


def prepare_cancel(
    conn: sqlite3.Connection, session_id: str, mode: str, intent: Intent, raw_message: str
) -> dict[str, Any]:
    active = conn.execute(
        "SELECT * FROM subscriptions WHERE user_id = ? AND status = 'active'", (USER_ID,)
    ).fetchall()
    matches = [row for row in active if row["merchant"] in (intent.subscription or raw_message)]
    if not matches and intent.subscription:
        matches = [row for row in active if intent.subscription in row["merchant"]]
    if not matches:
        set_context(conn, session_id, {"pending_cancel": True})
        names = "、".join(row["merchant"] for row in active)
        return reply(f"请指定要取消的订阅。当前生效：{names or '无'}。", session_id, mode)
    if len(matches) != 1:
        set_context(conn, session_id, {"pending_cancel": True})
        return reply("找到多个匹配的订阅，请说出完整商户名称。", session_id, mode)
    sub = matches[0]
    payload = {
        "subscription_id": sub["id"],
        "merchant": sub["merchant"],
        "amount_yuan": money(sub["amount_cents"]),
        "renewal_on": sub["renewal_on"],
    }
    action = create_action(conn, session_id, "subscription_cancel", "yellow", payload)
    set_context(conn, session_id, {})
    return reply("请确认取消这项模拟代扣协议。确认后将不再生成后续自动扣费。", session_id, mode, pending_action=action)


def create_action(
    conn: sqlite3.Connection, session_id: str, action_type: str, tier: str, payload: dict[str, Any]
) -> dict[str, Any]:
    action_id = secrets.token_urlsafe(18)
    now = datetime.now(timezone.utc)
    expires = now + timedelta(minutes=10)
    conn.execute(
        "INSERT INTO actions (id, session_id, type, payload_json, tier, status, created_at, expires_at) "
        "VALUES (?, ?, ?, ?, ?, 'pending', ?, ?)",
        (action_id, session_id, action_type, json.dumps(payload, ensure_ascii=False), tier,
         now.isoformat(timespec="seconds"), expires.isoformat(timespec="seconds")),
    )
    audit(conn, session_id, "action_prepared", {"action_id": action_id, "type": action_type, "tier": tier})
    return {"id": action_id, "type": action_type, "tier": tier, "status": "pending", "details": payload,
            "expires_at": expires.isoformat(timespec="seconds")}


def confirm_action(action_id: str, session_id: str) -> dict[str, Any]:
    conn = connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute("SELECT * FROM actions WHERE id = ?", (action_id,)).fetchone()
        if row is None or row["session_id"] != session_id:
            raise HTTPException(status_code=404, detail="未找到该待确认操作")
        if row["status"] == "completed":
            conn.commit()
            return json.loads(row["result_json"])
        if row["status"] != "pending":
            raise HTTPException(status_code=409, detail="该操作已失效")
        if datetime.fromisoformat(row["expires_at"]) < datetime.now(timezone.utc):
            conn.execute("UPDATE actions SET status = 'expired' WHERE id = ?", (action_id,))
            audit(conn, session_id, "action_expired", {"action_id": action_id})
            conn.commit()
            raise HTTPException(status_code=409, detail="确认已过期，请重新发起")
        if row["tier"] == "red":
            try:
                require_verified(conn, row)
            except HTTPException:
                audit(conn, session_id, "strong_verification_required", {"action_id": action_id})
                conn.commit()
                raise
        payload = json.loads(row["payload_json"])
        if row["type"] == "transfer":
            result = execute_transfer(conn, action_id, session_id, payload)
        elif row["type"] == "aa_collection":
            from .aa import create_collection
            result = create_collection(conn, action_id, session_id, payload)
        elif row["type"] == "aa_refund":
            from .aa import execute_refund
            result = execute_refund(conn, action_id, session_id, payload)
        elif row["type"] == "aa_settlement":
            from .aa_settlement import execute_create
            result = execute_create(conn, action_id, session_id, payload)
        elif row["type"] == "aa_settlement_leg":
            from .aa_settlement import execute_leg
            result = execute_leg(conn, action_id, session_id, payload)
        elif row["type"] == "scheduled_transfer":
            from .schedules import create_schedule
            result = create_schedule(conn, action_id, session_id, payload)
        elif row["type"] == "subscription_cancel":
            result = execute_cancel(conn, action_id, session_id, payload)
        elif row["type"] == "subscription_batch":
            from .subscription_intelligence import execute_batch
            result = execute_batch(conn, action_id, session_id, payload)
        elif row["type"] == "spending_plan":
            from .plans import execute_plan
            result = execute_plan(conn, action_id, session_id, payload)
        elif row["type"] in ("fund_reserve", "fund_release"):
            from .execution_controls import execute_reserve, execute_release
            result = (execute_reserve if row["type"] == "fund_reserve" else execute_release)(conn, action_id, session_id, payload)
        elif row["type"] in ("investment_buy", "investment_redeem"):
            from .investments import execute_buy, execute_redeem
            result = (execute_buy if row["type"] == "investment_buy" else execute_redeem)(conn, action_id, session_id, payload)
        elif row["type"] in ("card_update", "card_application", "card_payment"):
            from .cards import execute_update, execute_application, execute_payment
            result = {"card_update": execute_update, "card_application": execute_application, "card_payment": execute_payment}[row['type']](conn, action_id, session_id, payload)
        elif row['type'] in ('birthday_task', 'birthday_cancel', 'birthday_order_cancel'):
            from .life_tasks import execute_create, execute_cancel as cancel_life, execute_order_cancel
            result = {'birthday_task': execute_create, 'birthday_cancel': cancel_life, 'birthday_order_cancel': execute_order_cancel}[row['type']](conn, action_id, session_id, payload)
        elif row['type'] in ('alias_upsert', 'alias_delete'):
            from .aliases import execute_upsert, execute_delete
            result = (execute_upsert if row['type']=='alias_upsert' else execute_delete)(conn, action_id, session_id, payload)
        elif row['type'] in ('recurring_transfer', 'batch_transfer', 'recurring_cancel'):
            from .recurring import execute_recurring, execute_batch as batch_transfer, execute_cancel as cancel_recurring
            result = {'recurring_transfer':execute_recurring,'batch_transfer':batch_transfer,'recurring_cancel':cancel_recurring}[row['type']](conn,action_id,session_id,payload)
        elif row['type'] in ('bill_classification', 'bill_budget_upsert', 'bill_budget_delete'):
            from .bill_preferences import execute_classification, execute_budget, execute_delete as delete_budget
            result = {'bill_classification':execute_classification,'bill_budget_upsert':execute_budget,'bill_budget_delete':delete_budget}[row['type']](conn,action_id,session_id,payload)
        else:
            raise HTTPException(status_code=400, detail="不支持的操作类型")
        conn.execute(
            "UPDATE actions SET status = 'completed', result_json = ? WHERE id = ?",
            (json.dumps(result, ensure_ascii=False), action_id),
        )
        audit(conn, session_id, "action_completed", {"action_id": action_id, "type": row["type"]})
        conn.commit()
        return result
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def execute_transfer(
    conn: sqlite3.Connection, action_id: str, session_id: str, payload: dict[str, Any],
    *, authorization_id: str | None = None,
) -> dict[str, Any]:
    parent_tier = None
    if authorization_id:
        from .recurring import validate_transfer_authorization
        parent_tier = validate_transfer_authorization(conn, authorization_id, action_id, session_id, payload)
    contact = conn.execute(
        "SELECT * FROM contacts WHERE id = ? AND user_id = ? AND verified = 1",
        (payload["contact_id"], USER_ID),
    ).fetchone()
    if contact is None:
        raise HTTPException(status_code=409, detail="收款人已不可用，未转账")
    if payload.get("contact_fingerprint") and payload["contact_fingerprint"] != contact_fingerprint(contact):
        raise HTTPException(status_code=409, detail="收款人信息已变化，请重新核对并预约，未转账")
    amount = int(payload["amount_cents"])
    sent_today = conn.execute(
        "SELECT COALESCE(SUM(amount_cents), 0) FROM transactions "
        "WHERE account_id = ? AND posted_on = ? AND category IN ('转账','AA结算') AND direction = 'out'",
        (ACCOUNT_ID, business_date(conn)),
    ).fetchone()[0]
    if sent_today + amount > 100_000:
        if parent_tier != 'red':
            action = conn.execute("SELECT * FROM actions WHERE id=? AND session_id=?", (action_id, session_id)).fetchone()
            if action is None or action['tier'] != 'red' or action['type'] != 'transfer':
                raise HTTPException(status_code=403, detail="日累计超过 ¥1000，请重新生成计划并完成强验证，未转账")
            require_verified(conn, action)
    debit(conn, amount)
    tx_id = f"transfer-{action_id}"
    conn.execute(
        "INSERT INTO transactions (id, account_id, posted_on, direction, amount_cents, counterparty, "
        "category, note, action_id) VALUES (?, ?, ?, 'out', ?, ?, '转账', ?, ?)",
        (tx_id, ACCOUNT_ID, business_date(conn), amount, contact["name"], payload["note"], action_id),
    )
    return {"status": "completed", "message": f"模拟转账成功：向 {contact['name']}（{mask_phone(contact['phone'])}）转出 ¥{money(amount)}。",
            "transaction_id": tx_id, "action_id": action_id}


def execute_cancel(
    conn: sqlite3.Connection, action_id: str, session_id: str, payload: dict[str, Any]
) -> dict[str, Any]:
    sub = conn.execute("SELECT * FROM subscriptions WHERE id=? AND user_id=?", (payload['subscription_id'], USER_ID)).fetchone()
    if sub is None or sub['merchant'] != payload['merchant'] or money(sub['amount_cents']) != payload['amount_yuan'] or sub['renewal_on'] != payload['renewal_on']:
        raise HTTPException(409, "代扣协议内容已变化，请重新核对")
    changed = conn.execute(
        "UPDATE subscriptions SET status = 'cancelled' "
        "WHERE id = ? AND user_id = ? AND status = 'active'",
        (payload["subscription_id"], USER_ID),
    ).rowcount
    if changed != 1:
        raise HTTPException(status_code=409, detail="代扣协议状态已变化，未重复取消")
    conn.execute("INSERT OR REPLACE INTO subscription_closures VALUES (?, ?)",
                 (payload['subscription_id'], business_now(conn).isoformat(timespec='seconds')))
    return {"status": "completed", "message": f"已取消 {payload['merchant']} 的模拟代扣协议。",
            "subscription_id": payload["subscription_id"], "action_id": action_id}
