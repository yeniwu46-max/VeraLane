"""Typed read-only bill insight HTTP boundary."""

from fastapi import APIRouter, Query
from pydantic import BaseModel, ConfigDict, Field

from .insights import get_insights, query_insights


router = APIRouter(prefix="/api/insights", tags=["insights"])


class InsightQuery(BaseModel):
    model_config = ConfigDict(extra="forbid")
    session_id: str = Field(min_length=1, max_length=100)
    message: str = Field(min_length=1, max_length=500)
    period: str = Field(default="本月", min_length=1, max_length=20)


@router.get("")
def read_insights(period: str = Query(default="本月", min_length=1, max_length=20)):
    return get_insights(period)


@router.post("/query")
def ask_insights(body: InsightQuery):
    return query_insights(body.session_id, body.message, body.period)
