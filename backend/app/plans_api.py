"""HTTP routes for finite spending optimization plans."""

from fastapi import APIRouter, Query
from pydantic import BaseModel, ConfigDict, Field

from .plans import cancel_plan, get_plan, invalidate_selection, list_plans, prepare_plan, preview_plan


router = APIRouter(prefix="/api/plans", tags=["plans"])


class PlanOwner(BaseModel):
    model_config = ConfigDict(extra="forbid")
    session_id: str = Field(min_length=1, max_length=100)


class PlanPreview(PlanOwner):
    message: str = Field(min_length=1, max_length=500)


class PlanSelection(PlanOwner):
    subscription_ids: list[str] = Field(min_length=1, max_length=10)


@router.get("")
def read_plans(session_id: str = Query(min_length=1, max_length=100)):
    return list_plans(session_id)


@router.post("/preview")
def preview(body: PlanPreview):
    return preview_plan(body.session_id, body.message)


@router.get("/{plan_id}")
def read_plan(plan_id: str, session_id: str = Query(min_length=1, max_length=100)):
    return get_plan(plan_id, session_id)


@router.post("/{plan_id}/prepare")
def prepare(plan_id: str, body: PlanSelection):
    return prepare_plan(plan_id, body.session_id, body.subscription_ids)


@router.post("/{plan_id}/cancel")
def cancel(plan_id: str, body: PlanOwner):
    return cancel_plan(plan_id, body.session_id)


@router.post("/{plan_id}/invalidate")
def invalidate(plan_id: str, body: PlanOwner):
    return invalidate_selection(plan_id, body.session_id)
