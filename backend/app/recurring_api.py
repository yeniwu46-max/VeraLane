"""Strict adapters for finite recurring and batch transfer plans."""
from fastapi import APIRouter, Query
from pydantic import BaseModel, ConfigDict, Field
from typing import Literal
from . import recurring

router = APIRouter(prefix="/api/transfers", tags=["Recurring and batch transfers"])


class Owner(BaseModel):
    model_config = ConfigDict(extra="forbid")
    session_id: str = Field(min_length=1, max_length=100)


class Item(BaseModel):
    model_config = ConfigDict(extra="forbid")
    contact_id: str = Field(min_length=1, max_length=100)
    amount_yuan: str = Field(min_length=1, max_length=30)
    note: str = Field(default="转账", max_length=100)


class RecurringRequest(Owner):
    contact_id: str = Field(min_length=1, max_length=100)
    amount_yuan: str = Field(min_length=1, max_length=30)
    note: str = Field(default="周期转账", max_length=100)
    first_at: str = Field(min_length=16, max_length=30)
    monthly_day: int = Field(ge=1, le=31, strict=True)
    count: int = Field(ge=1, le=12, strict=True)


class BatchRequest(Owner):
    items: list[Item] = Field(min_length=2, max_length=10)


class InterpretRequest(Owner):
    message: str = Field(min_length=1, max_length=500)
    kind: Literal["recurring", "batch"]


@router.post("/interpret")
def interpret(request: InterpretRequest):
    return recurring.interpret(request.session_id, request.message, request.kind)


@router.post("/recurring/preview")
def preview_recurring(request: RecurringRequest):
    return recurring.preview_recurring(request.model_dump())


@router.post("/recurring/prepare")
def prepare_recurring(request: RecurringRequest):
    return recurring.prepare_recurring(request.model_dump())


@router.get("/recurring")
def recurring_list(session_id: str = Query(min_length=1, max_length=100)):
    return recurring.list_recurring(session_id)


@router.get("/recurring/{id}")
def recurring_detail(id: str, session_id: str = Query(min_length=1, max_length=100)):
    return recurring.get_recurring(id, session_id)


@router.post("/recurring/{id}/cancel/prepare")
def cancel_recurring(id: str, request: Owner):
    return recurring.prepare_cancel(id, request.session_id)


@router.post("/batch/preview")
def preview_batch(request: BatchRequest):
    return recurring.preview_batch(request.model_dump())


@router.post("/batch/prepare")
def prepare_batch(request: BatchRequest):
    return recurring.prepare_batch(request.model_dump())


@router.get("/batch")
def batches(session_id: str = Query(min_length=1, max_length=100)):
    return recurring.list_batches(session_id)


@router.get("/batch/{id}")
def batch_detail(id: str, session_id: str = Query(min_length=1, max_length=100)):
    return recurring.get_batch(id, session_id)
