from __future__ import annotations

from decimal import Decimal, InvalidOperation

from fastapi import HTTPException

from .db import USER_ID, db_session
from .service import money

MAX_TOTAL_CENTS = 10_000_000
MAX_EXPENSES = 100


def _cents(value: str) -> int:
    try:
        amount = Decimal(value)
        if not amount.is_finite() or amount <= 0 or amount.as_tuple().exponent < -2:
            raise ValueError
        cents = int(amount * 100)
        if cents <= 0 or cents > MAX_TOTAL_CENTS:
            raise ValueError
        return cents
    except (InvalidOperation, ValueError, TypeError):
        raise HTTPException(422, "每笔垫付金额须大于 0、精确到分，且不超过 ¥100,000") from None


def calculate_preview(participants: list[dict], expenses: list[dict], note: str = "") -> dict:
    if not 2 <= len(participants) <= 8:
        raise HTTPException(422, "结算参与人须为 2–8 人，包含本人")
    participant_ids = [item.get("id") for item in participants]
    if len(set(participant_ids)) != len(participant_ids) or "self" not in participant_ids:
        raise HTTPException(422, "参与人必须包含本人且不能重复")
    if not 1 <= len(expenses) <= MAX_EXPENSES:
        raise HTTPException(422, "请提供 1–100 笔垫付记录")

    by_id = {item["id"]: item for item in participants}
    paid = {participant_id: 0 for participant_id in participant_ids}
    normalized_expenses = []
    total = 0
    for expense in expenses:
        payer_id = expense.get("payer_id")
        if payer_id not in by_id:
            raise HTTPException(422, "每笔支出都必须指定本次结算参与人")
        cents = _cents(expense.get("amount_yuan", ""))
        total += cents
        if total > MAX_TOTAL_CENTS:
            raise HTTPException(422, "垫付总额不能超过 ¥100,000")
        paid[payer_id] += cents
        normalized_expenses.append({"payer_id": payer_id, "payer_name": by_id[payer_id]["name"],
                                    "amount_yuan": money(cents), "note": (expense.get("note") or "")[:100]})

    share, remainder = divmod(total, len(participants))
    participant_results = []
    net = {}
    for index, participant in enumerate(participants):
        share_cents = share + (1 if index < remainder else 0)
        balance_cents = paid[participant["id"]] - share_cents
        net[participant["id"]] = balance_cents
        participant_results.append({
            **participant,
            "paid_cents": paid[participant["id"]], "paid_yuan": money(paid[participant["id"]]),
            "share_cents": share_cents, "share_yuan": money(share_cents),
            "net_cents": balance_cents, "net_yuan": money(balance_cents),
            "direction": "receive" if balance_cents > 0 else "pay" if balance_cents < 0 else "settled",
        })

    debtors = [[person_id, -amount] for person_id, amount in net.items() if amount < 0]
    creditors = [[person_id, amount] for person_id, amount in net.items() if amount > 0]
    transfers = []
    debtor_index = creditor_index = 0
    while debtor_index < len(debtors) and creditor_index < len(creditors):
        debtor_id, debt = debtors[debtor_index]
        creditor_id, credit = creditors[creditor_index]
        amount = min(debt, credit)
        transfers.append({"from_id": debtor_id, "from_name": by_id[debtor_id]["name"],
                          "to_id": creditor_id, "to_name": by_id[creditor_id]["name"],
                          "amount_cents": amount, "amount_yuan": money(amount)})
        debtors[debtor_index][1] -= amount
        creditors[creditor_index][1] -= amount
        if debtors[debtor_index][1] == 0:
            debtor_index += 1
        if creditors[creditor_index][1] == 0:
            creditor_index += 1

    if any(value for _, value in debtors[debtor_index:]) or any(value for _, value in creditors[creditor_index:]):
        raise HTTPException(500, "结算金额无法平衡，请检查输入")
    return {"note": (note.strip() or "多人垫付结算")[:100], "total_cents": total, "total_yuan": money(total),
            "expense_count": len(normalized_expenses), "participants": participant_results,
            "expenses": normalized_expenses,
            "transfers": transfers,
            "notice": "这是本地计算的结算建议，不会发起转账、发送催收消息或改变任何账户余额。"}


def preview_for_session(contact_ids: list[str], expenses: list[dict], note: str = "") -> dict:
    if not 1 <= len(contact_ids) <= 7 or len(set(contact_ids)) != len(contact_ids):
        raise HTTPException(422, "请选择 1–7 位不重复的已验证联系人，连同本人共 2–8 人")
    with db_session() as conn:
        participants = [{"id": "self", "name": "我"}]
        for contact_id in contact_ids:
            contact = conn.execute("SELECT id, name FROM contacts WHERE id = ? AND user_id = ? AND verified = 1",
                                   (contact_id, USER_ID)).fetchone()
            if contact is None:
                raise HTTPException(422, "参与人必须来自已验证联系人")
            participants.append({"id": contact["id"], "name": contact["name"]})
        return calculate_preview(participants, expenses, note)
