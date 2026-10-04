"""HTTP entry point for the local VeraLane prototype."""

from __future__ import annotations

import os
import uuid
import csv
import io
import json
import asyncio
import logging
from contextlib import asynccontextmanager
from typing import Literal

from fastapi import FastAPI, Response, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field

from .db import init_db, ROOT
from .service import (
    confirm_action, contact_options, direct_bill_report, direct_prepare_cancel,
    direct_prepare_transfer, overview, process_message, resolve_transfer_contact,
)
from .schedules import advance_to_next, cancel_schedule, list_schedules, run_due_transfers
from . import aa, aa_settlement
from .subscription_intelligence import router as subscription_router
from .insights_api import router as insights_router
from .plans_api import router as plans_router
from .controls_api import router as controls_router
from .investments_api import router as investments_router
from .cards_api import router as cards_router
from .life_tasks_api import router as life_router
from .aliases_api import router as aliases_router
from .recurring_api import router as recurring_router
from .bill_preferences_api import router as bill_preferences_router


async def scheduler_loop():
    while True:
        try:
            await asyncio.to_thread(run_due_transfers)
        except Exception:
            logging.exception("Scheduled transfer scan failed; transaction was rolled back")
        await asyncio.sleep(2)


@asynccontextmanager
async def lifespan(_: FastAPI):
    init_db()
    worker = asyncio.create_task(scheduler_loop())
    try:
        yield
    finally:
        worker.cancel()
        try:
            await worker
        except asyncio.CancelledError:
            pass


app = FastAPI(title="VeraLane Demo API", version="0.2.0", lifespan=lifespan)
app.include_router(subscription_router)
app.include_router(insights_router)
app.include_router(plans_router)
app.include_router(controls_router)
app.include_router(investments_router)
app.include_router(cards_router)
app.include_router(life_router)
app.include_router(aliases_router)
app.include_router(recurring_router)
app.include_router(bill_preferences_router)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
    allow_methods=["GET", "POST"],
    allow_headers=["Content-Type"],
)


class ChatRequest(BaseModel):
    session_id: str | None = Field(default=None, max_length=100)
    message: str = Field(min_length=1, max_length=500)


class ConfirmRequest(BaseModel):
    session_id: str = Field(min_length=1, max_length=100)


class DirectTransferRequest(BaseModel):
    session_id: str = Field(min_length=1, max_length=100)
    contact_id: str = Field(min_length=1, max_length=100)
    amount_yuan: str = Field(min_length=1, max_length=30)
    note: str = Field(default="转账", max_length=100)


class ResolveTransferRequest(BaseModel):
    session_id: str = Field(min_length=1, max_length=100)
    contact_id: str = Field(min_length=1, max_length=100)


class DirectCancelRequest(BaseModel):
    session_id: str = Field(min_length=1, max_length=100)


class AaOwnerRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    session_id: str = Field(min_length=1, max_length=100)


class AaInterpretRequest(AaOwnerRequest):
    message: str = Field(min_length=1, max_length=500)
    source_transaction_id: str | None = Field(default=None, max_length=200)
    reset: bool = False


class AaPlanRequest(AaOwnerRequest):
    total_yuan: str = Field(min_length=1, max_length=30)
    contact_ids: list[str] = Field(min_length=1, max_length=7)
    include_self: bool
    note: str = Field(default="AA 分摊", max_length=100)
    source_transaction_id: str | None = Field(default=None, max_length=200)
    shares_yuan: dict[str, str] | None = Field(default=None, max_length=8)
    shares_ratio: dict[str, int] | None = Field(default=None, max_length=8)


class AaSettlementExpenseRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    payer_id: str = Field(min_length=1, max_length=100)
    amount_yuan: str = Field(min_length=1, max_length=30)
    note: str = Field(default="", max_length=100)


class AaSettlementRequest(AaOwnerRequest):
    contact_ids: list[str] = Field(min_length=1, max_length=7)
    expenses: list[AaSettlementExpenseRequest] = Field(min_length=1, max_length=100)
    note: str = Field(default="多人垫付结算", max_length=100)


class AaInstallmentRequest(AaOwnerRequest):
    amount_yuan: str = Field(min_length=1, max_length=30)
    idempotency_key: str = Field(min_length=1, max_length=100)


class AaRefundRequest(AaOwnerRequest):
    source_transaction_id: str = Field(min_length=1, max_length=200)
    amount_yuan: str = Field(min_length=1, max_length=30)


@app.get("/api/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/api/overview")
def get_overview() -> dict:
    from .model_budget import get_model_status
    model_status = get_model_status()
    return {**overview(), "model_configured": bool(os.environ.get("DEEPSEEK_API_KEY", "").strip()), "model_status": model_status}


@app.get("/api/contacts")
def get_contacts() -> list[dict]:
    return contact_options()


@app.get("/api/bills")
def get_bill(period: Literal["本月", "上个月", "今年", "去年"] = "本月") -> dict:
    return direct_bill_report(period)


@app.get("/api/bills/export")
def export_bill(
    period: Literal["本月", "上个月", "今年", "去年"] = "本月",
    format: Literal["csv", "json"] = "csv",
) -> Response:
    report = direct_bill_report(period)
    filename = f"VeraLane-bill-{report['period']}.{format}"
    headers = {"Content-Disposition": f'attachment; filename="{filename}"'}
    if format == "json":
        return Response(
            content=json.dumps(report, ensure_ascii=False, indent=2),
            media_type="application/json; charset=utf-8", headers=headers,
        )

    def cell(value: object) -> str:
        raw = str(value)
        return f"'{raw}" if raw.startswith(("=", "+", "-", "@", "\t", "\r")) else raw

    output = io.StringIO(newline="")
    writer = csv.writer(output)
    writer.writerow(["报告期间", report["period"]])
    writer.writerow(["总支出（元）", report["total_yuan"]])
    writer.writerow([])
    writer.writerow(["分类", "支出金额（元）"])
    writer.writerows([[cell(row["name"]), row["amount_yuan"]] for row in report["categories"]])
    writer.writerow([])
    writer.writerow(["交易日期", "交易编号", "交易对象", "分类", "备注", "支出金额（元）", "原始分类", "归类原因", "归类版本"])
    writer.writerows([
        [tx["posted_on"], cell(tx["id"]), cell(tx["counterparty"]),
         cell(tx["category"]), cell(tx["note"]), tx["amount_yuan"], cell(tx.get('original_category','')),
         cell(tx.get('classification_reason') or ''), tx.get('classification_version') or '']
        for tx in report["transactions"]
    ])
    return Response(content="\ufeff" + output.getvalue(), media_type="text/csv; charset=utf-8", headers=headers)


@app.post("/api/transfers/prepare")
def prepare_direct_transfer(request: DirectTransferRequest) -> dict:
    return direct_prepare_transfer(request.session_id, request.contact_id, request.amount_yuan, request.note)


@app.post("/api/transfers/resolve")
def resolve_transfer(request: ResolveTransferRequest) -> dict:
    return resolve_transfer_contact(request.session_id, request.contact_id)


@app.post("/api/subscriptions/{subscription_id}/prepare-cancel")
def prepare_direct_cancel(subscription_id: str, request: DirectCancelRequest) -> dict:
    return direct_prepare_cancel(request.session_id, subscription_id)


@app.post("/api/chat")
async def chat(request: ChatRequest) -> dict:
    session_id = request.session_id or str(uuid.uuid4())
    return await process_message(session_id, request.message.strip())


@app.post("/api/actions/{action_id}/confirm")
def confirm(action_id: str, request: ConfirmRequest) -> dict:
    return confirm_action(action_id, request.session_id)


@app.get("/api/schedules")
def schedules(session_id: str = Query(min_length=1, max_length=100)) -> dict:
    return list_schedules(session_id)


@app.post("/api/schedules/{task_id}/cancel")
def cancel_scheduled(task_id: str, request: ConfirmRequest) -> dict:
    return cancel_schedule(task_id, request.session_id)


@app.post("/api/demo/clock/advance-next")
def advance_demo_clock(request: ConfirmRequest) -> dict:
    return advance_to_next(request.session_id)


@app.get('/api/demo/events')
def demo_events(session_id: str = Query(min_length=1, max_length=100)):
    from .jobs import events_snapshot
    return events_snapshot(session_id)


@app.post("/api/aa/interpret")
async def interpret_aa(request: AaInterpretRequest) -> dict:
    return await aa.interpret(request.session_id, request.message, request.source_transaction_id, request.reset)


@app.post("/api/aa/preview")
def preview_aa(request: AaPlanRequest) -> dict:
    return aa.preview(request.model_dump())


@app.post("/api/aa/prepare")
def prepare_aa(request: AaPlanRequest) -> dict:
    return aa.prepare(request.model_dump())


@app.post("/api/aa/settlements/preview")
def preview_aa_settlement(request: AaSettlementRequest) -> dict:
    return aa_settlement.preview_for_session(request.contact_ids, [expense.model_dump() for expense in request.expenses], request.note)


@app.get("/api/aa/collections")
def list_aa(session_id: str = Query(min_length=1, max_length=100)) -> dict:
    return aa.list_collections(session_id)


@app.get("/api/aa/collections/{collection_id}")
def read_aa(collection_id: str, session_id: str = Query(min_length=1, max_length=100)) -> dict:
    return aa.get_collection(collection_id, session_id)


@app.post("/api/aa/collections/{collection_id}/close")
def close_aa(collection_id: str, request: AaOwnerRequest) -> dict:
    return aa.close_collection(collection_id, request.session_id)


@app.post("/api/aa/requests/{request_id}/simulate-payment")
def pay_aa(request_id: str, request: AaOwnerRequest) -> dict:
    return aa.simulate_payment(request_id, request.session_id)


@app.post("/api/aa/requests/{request_id}/installments")
def pay_aa_installment(request_id: str, request: AaInstallmentRequest) -> dict:
    return aa.simulate_installment(request_id, request.session_id, request.amount_yuan, request.idempotency_key)


@app.post("/api/aa/requests/{request_id}/refund/prepare")
def prepare_aa_refund(request_id: str, request: AaRefundRequest) -> dict:
    return aa.prepare_refund(request_id, request.session_id, request.source_transaction_id, request.amount_yuan)


# Registered last: API routes take precedence. Only the built public UI is served.
if (ROOT / 'frontend' / 'dist' / 'index.html').is_file():
    app.mount('/', StaticFiles(directory=ROOT / 'frontend' / 'dist', html=True), name='frontend')
