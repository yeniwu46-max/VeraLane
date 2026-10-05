"""HTTP entry point for the local VeraLane prototype."""

from __future__ import annotations

import os
import uuid
import csv
import io
import json
import asyncio
import logging
import sqlite3
from ipaddress import ip_address
from contextlib import asynccontextmanager
from typing import Literal
from urllib.parse import urlsplit

from fastapi import FastAPI, Response, Query, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field
from starlette.datastructures import MutableHeaders
from starlette.middleware.trustedhost import TrustedHostMiddleware

from . import db
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
from .receipt_ocr import MAX_MULTIPART_BODY_BYTES, router as receipt_ocr_router


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


class SecurityHeadersMiddleware:
    """Wrap the full ASGI stack so framework-generated 500 responses are covered."""

    def __init__(self, application):
        self.application = application

    def __getattr__(self, name):
        # Keep FastAPI inspection helpers available to tests and local tooling.
        return getattr(self.application, name)

    async def __call__(self, scope, receive, send):
        async def send_with_security_headers(message):
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                headers["Content-Security-Policy"] = (
                    "default-src 'self'; base-uri 'self'; object-src 'none'; frame-ancestors 'none'; "
                    "form-action 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
                    "img-src 'self' data: blob:; font-src 'self' data:; connect-src 'self'"
                )
                headers["X-Content-Type-Options"] = "nosniff"
                headers["X-Frame-Options"] = "DENY"
                headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
                headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
                if scope.get("scheme") == "https":
                    headers["Strict-Transport-Security"] = "max-age=31536000"
            await send(message)

        await self.application(scope, receive, send_with_security_headers)


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
app.include_router(receipt_ocr_router)


class ReceiptBodyLimitMiddleware:
    """Reject oversized receipt requests before multipart parsing can spool them."""

    def __init__(self, application):
        self.application = application

    async def __call__(self, scope, receive, send):
        if (scope.get("type") != "http" or scope.get("path") != "/api/aa/ocr/receipt"
                or scope.get("method") != "POST"):
            await self.application(scope, receive, send)
            return

        content_length = next(
            (value for name, value in scope.get("headers", []) if name.lower() == b"content-length"),
            None,
        )
        if content_length is not None:
            try:
                if int(content_length) > MAX_MULTIPART_BODY_BYTES:
                    await self._reject(send)
                    return
            except ValueError:
                pass

        received = 0

        async def receive_bounded():
            nonlocal received
            message = await receive()
            if message.get("type") == "http.request":
                received += len(message.get("body", b""))
                if received > MAX_MULTIPART_BODY_BYTES:
                    raise _ReceiptBodyTooLarge
            return message

        try:
            await self.application(scope, receive_bounded, send)
        except _ReceiptBodyTooLarge:
            await self._reject(send)

    @staticmethod
    async def _reject(send):
        body = '{"detail":"小票上传请求过大；请将图片压缩至 900 KB 以内"}'.encode("utf-8")
        await send({"type": "http.response.start", "status": 413, "headers": [
            (b"content-type", b"application/json; charset=utf-8"),
            (b"content-length", str(len(body)).encode("ascii")),
        ]})
        await send({"type": "http.response.body", "body": body})


class _ReceiptBodyTooLarge(Exception):
    pass


app.add_middleware(ReceiptBodyLimitMiddleware)

ALLOWED_FRONTEND_ORIGINS = frozenset({"http://localhost:5173", "http://127.0.0.1:5173"})


class BrowserWriteOriginMiddleware:
    """Reject browser-originated writes from sites outside the local demo UI."""

    def __init__(self, application):
        self.application = application

    @staticmethod
    def _origin_parts(value: str):
        parsed = urlsplit(value)
        if parsed.scheme not in ("http", "https") or not parsed.hostname:
            return None
        if parsed.username or parsed.password or parsed.path or parsed.query or parsed.fragment:
            return None
        try:
            port = parsed.port or (443 if parsed.scheme == "https" else 80)
        except ValueError:
            return None
        return parsed.scheme.lower(), parsed.hostname.rstrip(".").lower(), port

    async def __call__(self, scope, receive, send):
        if scope.get("type") == "http" and scope.get("method") in {"POST", "PUT", "PATCH", "DELETE"}:
            headers = scope.get("headers", [])
            origins = [value.decode("latin-1") for name, value in headers if name.lower() == b"origin"]
            if origins:
                origin = origins[0]
                request_host = next((value.decode("latin-1") for name, value in headers
                                     if name.lower() == b"host"), "")
                origin_parts = self._origin_parts(origin)
                same_local_origin = False
                if len(origins) == 1 and origin_parts:
                    request_parts = self._origin_parts(f"{scope.get('scheme', 'http')}://{request_host}")
                    hostname = request_parts[1] if request_parts else ""
                    try:
                        is_loopback = hostname == "localhost" or ip_address(hostname).is_loopback
                    except ValueError:
                        is_loopback = False
                    same_local_origin = is_loopback and origin_parts == request_parts

                if len(origins) != 1 or (origin not in ALLOWED_FRONTEND_ORIGINS and not same_local_origin):
                    body = '{"detail":"跨站写请求已拒绝"}'.encode("utf-8")
                    await send({"type": "http.response.start", "status": 403, "headers": [
                        (b"content-type", b"application/json; charset=utf-8"),
                        (b"content-length", str(len(body)).encode("ascii")),
                    ]})
                    await send({"type": "http.response.body", "body": body})
                    return
        await self.application(scope, receive, send)


app.add_middleware(
    CORSMiddleware,
    allow_origins=sorted(ALLOWED_FRONTEND_ORIGINS),
    allow_methods=["GET", "POST"],
    allow_headers=["Content-Type"],
)
app.add_middleware(BrowserWriteOriginMiddleware)
# Validate every Host before serving even read-only endpoints, so a DNS-rebound
# attacker domain cannot make the browser treat the local API as same-origin.
app.add_middleware(
    TrustedHostMiddleware,
    allowed_hosts=["localhost", "127.0.0.1", "[::1]"],
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


class AaItemizedLineRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    description: str = Field(min_length=1, max_length=80)
    amount_yuan: str = Field(min_length=1, max_length=30)
    participant_ids: list[str] = Field(min_length=1, max_length=8)


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
    itemized_items: list[AaItemizedLineRequest] | None = Field(default=None, max_length=100)


class AaSettlementExpenseRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    payer_id: str = Field(min_length=1, max_length=100)
    amount_yuan: str = Field(min_length=1, max_length=30)
    note: str = Field(default="", max_length=100)


class AaSettlementRequest(AaOwnerRequest):
    contact_ids: list[str] = Field(min_length=1, max_length=7)
    expenses: list[AaSettlementExpenseRequest] = Field(min_length=1, max_length=100)
    note: str = Field(default="多人垫付结算", max_length=100)


class AaSettlementLegRequest(AaOwnerRequest):
    pass


class AaInstallmentRequest(AaOwnerRequest):
    amount_yuan: str = Field(min_length=1, max_length=30)
    idempotency_key: str = Field(min_length=1, max_length=100)


class AaRefundRequest(AaOwnerRequest):
    source_transaction_id: str = Field(min_length=1, max_length=200)
    amount_yuan: str = Field(min_length=1, max_length=30)


@app.get("/api/health")
def health() -> dict[str, str]:
    if not db.DB_PATH.is_file():
        raise HTTPException(503, "本地模拟账本暂不可用")
    try:
        with db.db_session() as conn:
            account = conn.execute("SELECT id FROM accounts WHERE id=?", (db.ACCOUNT_ID,)).fetchone()
            conn.execute("SELECT id FROM contacts WHERE user_id=? LIMIT 0", (db.USER_ID,)).fetchall()
            conn.execute("SELECT id FROM transactions WHERE account_id=? LIMIT 0", (db.ACCOUNT_ID,)).fetchall()
    except sqlite3.Error as exc:
        raise HTTPException(503, "本地模拟账本暂不可用") from exc
    if account is None:
        raise HTTPException(503, "本地模拟账户暂不可用")
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


@app.post("/api/aa/settlements/prepare")
def prepare_aa_settlement(request: AaSettlementRequest) -> dict:
    return aa_settlement.prepare_for_session(request.session_id, request.contact_ids,
                                            [expense.model_dump() for expense in request.expenses], request.note)


@app.get("/api/aa/settlements")
def list_aa_settlements(session_id: str = Query(min_length=1, max_length=100)) -> dict:
    return aa_settlement.list_settlements(session_id)


@app.get("/api/aa/settlements/{settlement_id}")
def get_aa_settlement(settlement_id: str, session_id: str = Query(min_length=1, max_length=100)) -> dict:
    return aa_settlement.get_settlement(settlement_id, session_id)


@app.post("/api/aa/settlements/{settlement_id}/legs/{leg_id}/prepare")
def prepare_aa_settlement_leg(settlement_id: str, leg_id: str, request: AaSettlementLegRequest) -> dict:
    return aa_settlement.prepare_leg(settlement_id, leg_id, request.session_id)


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

app = SecurityHeadersMiddleware(app)
