"""Validated routes for fictional investment operations."""

from typing import Annotated

from fastapi import APIRouter, Query
from pydantic import BaseModel, ConfigDict, Field

from . import investments


router = APIRouter(prefix="/api/investments", tags=["investments"])


class Owner(BaseModel):
    model_config = ConfigDict(extra="forbid")
    session_id: str = Field(min_length=1, max_length=100)


class AssessmentRequest(Owner):
    answers: list[Annotated[int, Field(strict=True, ge=1, le=3)]] = Field(min_length=5, max_length=5)


class BuyRequest(Owner):
    product_id: str = Field(min_length=1, max_length=100)
    amount_yuan: str = Field(min_length=1, max_length=30)
    horizon_days: int = Field(default=30, strict=True, ge=0, le=3650)
    liquidity_days: int = Field(default=2, strict=True, ge=0, le=365)


class RedeemRequest(Owner):
    holding_id: str = Field(min_length=1, max_length=100)
    units: str = Field(min_length=1, max_length=30)


class InterpretRequest(Owner):
    message: str = Field(min_length=1, max_length=500)


@router.get("/questions")
def questions():
    return investments.questions()


@router.post("/assessment")
def assessment(body: AssessmentRequest):
    return investments.assess(body.session_id, body.answers)


@router.get("/products")
def products(session_id: str = Query(min_length=1, max_length=100), horizon_days: int = Query(default=30, ge=0, le=3650), liquidity_days: int = Query(default=2, ge=0, le=365)):
    return investments.products(session_id, horizon_days, liquidity_days)


@router.get("/portfolio")
def portfolio(session_id: str = Query(min_length=1, max_length=100)):
    return investments.portfolio(session_id)


@router.post("/buy/prepare")
def buy(body: BuyRequest):
    return investments.prepare_buy(body.session_id, body.product_id, body.amount_yuan, body.horizon_days, body.liquidity_days)


@router.post("/redeem/prepare")
def redeem(body: RedeemRequest):
    return investments.prepare_redeem(body.session_id, body.holding_id, body.units)


@router.post("/interpret")
def interpret(body: InterpretRequest):
    return investments.interpret(body.session_id, body.message)
