"""Strict request adapters for the fixed birthday task workflow."""

from fastapi import APIRouter, Query
from pydantic import BaseModel, ConfigDict, Field

from . import life_tasks


router = APIRouter(prefix="/api/life", tags=["Birthday tasks"])


class Owner(BaseModel):
    model_config = ConfigDict(extra="forbid")
    session_id: str = Field(min_length=1, max_length=100)


class BirthdayRequest(Owner):
    goal: str = Field(min_length=1, max_length=120)
    birthday: str = Field(min_length=10, max_length=10)
    budget_yuan: str = Field(min_length=1, max_length=30)
    recipient_label: str = Field(min_length=1, max_length=60)
    delivery_note: str = Field(min_length=1, max_length=200)
    product_ids: list[str] = Field(min_length=1, max_length=2)


class InterpretRequest(Owner):
    message: str = Field(min_length=1, max_length=500)
    reset: bool = False


@router.post("/interpret")
def interpret(request: InterpretRequest):
    return life_tasks.interpret(request.session_id, request.message, request.reset)


@router.get("/catalog")
def catalog():
    return life_tasks.catalog()


@router.get("/tasks")
def list_tasks(session_id: str = Query(min_length=1, max_length=100)):
    return life_tasks.list_tasks(session_id)


@router.get("/tasks/{task_id}")
def task(task_id: str, session_id: str = Query(min_length=1, max_length=100)):
    return life_tasks.get_task(task_id, session_id)


@router.post("/tasks/prepare")
def prepare(request: BirthdayRequest):
    return life_tasks.prepare(request.model_dump())


@router.post("/tasks/{task_id}/cancel/prepare")
def prepare_cancel(task_id: str, request: Owner):
    return life_tasks.prepare_cancel(task_id, request.session_id)


@router.post("/orders/{order_id}/cancel/prepare")
def prepare_order_cancel(order_id: str, request: Owner):
    return life_tasks.prepare_order_cancel(order_id, request.session_id)
