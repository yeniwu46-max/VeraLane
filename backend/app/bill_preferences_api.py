"""Validated API for bill categorization and personal budgets."""

from fastapi import APIRouter, Query
from pydantic import BaseModel, ConfigDict, Field

from . import bill_preferences as service


router = APIRouter(prefix="/api/bill-preferences", tags=["bill-preferences"])


class Owner(BaseModel):
    model_config = ConfigDict(extra="forbid")
    session_id: str = Field(min_length=1, max_length=100)


class Classification(Owner):
    transaction_id: str = Field(min_length=1, max_length=200)
    category: str = Field(min_length=1, max_length=20)
    reason: str = Field(min_length=1, max_length=200)


class Budget(Owner):
    month: str = Field(pattern=r"^\d{4}-\d{2}$")
    category: str | None = Field(default=None, min_length=1, max_length=20)
    amount_yuan: str = Field(min_length=1, max_length=30)
    budget_id: str | None = Field(default=None, min_length=1, max_length=100)


@router.get("")
def read(session_id: str = Query(min_length=1, max_length=100), period: str = Query(default="本月", min_length=1, max_length=20)):
    return service.preferences(session_id, period)


@router.post("/classifications/prepare")
def classification(body: Classification):
    return service.prepare_classification(body.session_id, body.transaction_id, body.category, body.reason)


@router.post("/budgets/prepare")
def budget(body: Budget):
    return service.prepare_budget(body.session_id, body.month, body.category, body.amount_yuan, body.budget_id)


@router.post("/budgets/{budget_id}/delete/prepare")
def delete(budget_id: str, body: Owner):
    return service.prepare_delete_budget(body.session_id, budget_id)


@router.post("/actions/{action_id}/discard")
def discard(action_id: str, body: Owner):
    return service.discard_action(body.session_id, action_id)
