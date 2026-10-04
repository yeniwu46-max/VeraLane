"""HTTP adapters for fund reservation and simulated verification controls."""

from fastapi import APIRouter, Query
from pydantic import BaseModel, ConfigDict, Field

from . import execution_controls as controls
from .db import db_session


router = APIRouter(prefix="/api", tags=["Execution controls"])


class OwnerRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    session_id: str = Field(min_length=1, max_length=100)


class ReserveRequest(OwnerRequest):
    amount_yuan: str = Field(min_length=1, max_length=30)
    purpose: str = Field(min_length=1, max_length=100)


class VerifyRequest(OwnerRequest):
    challenge_id: str = Field(min_length=1, max_length=100)
    code: str = Field(min_length=1, max_length=20)


@router.get("/funds")
def list_funds(session_id: str = Query(min_length=1, max_length=100)):
    with db_session() as conn:
        return controls.funds_snapshot(conn, session_id)


@router.post("/funds/reservations/prepare")
def prepare_reservation(request: ReserveRequest):
    return controls.prepare_reserve(request.session_id, request.amount_yuan, request.purpose)


@router.post("/funds/reservations/{reservation_id}/release/prepare")
def prepare_release(reservation_id: str, request: OwnerRequest):
    return controls.prepare_release(request.session_id, reservation_id)


@router.post("/actions/{action_id}/challenge")
def issue_challenge(action_id: str, request: OwnerRequest):
    return controls.issue_challenge(action_id, request.session_id)


@router.post("/actions/{action_id}/verify")
def verify_challenge(action_id: str, request: VerifyRequest):
    return controls.verify_challenge(action_id, request.session_id, request.challenge_id, request.code)
