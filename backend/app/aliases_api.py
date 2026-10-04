"""Explicit alias maintenance API; mutations always prepare a consent action."""

from fastapi import APIRouter, Query
from pydantic import BaseModel, ConfigDict, Field

from . import aliases


router = APIRouter(prefix="/api/aliases", tags=["aliases"])


class Owner(BaseModel):
    model_config = ConfigDict(extra="forbid")
    session_id: str = Field(min_length=1, max_length=100)


class AliasRequest(Owner):
    alias: str = Field(min_length=1, max_length=20)
    contact_id: str = Field(min_length=1, max_length=100)
    alias_id: str | None = Field(default=None, min_length=1, max_length=100)


class InterpretRequest(Owner):
    message: str = Field(min_length=1, max_length=200)


@router.get("")
def read_aliases(session_id: str = Query(min_length=1, max_length=100)):
    return aliases.list_aliases(session_id)


@router.post("/prepare")
def prepare_alias(body: AliasRequest):
    return aliases.prepare_upsert(body.session_id, body.alias, body.contact_id, body.alias_id)


@router.post("/{alias_id}/delete/prepare")
def delete_alias(alias_id: str, body: Owner):
    return aliases.prepare_delete(body.session_id, alias_id)


@router.post("/interpret")
def interpret_alias(body: InterpretRequest):
    return aliases.interpret(body.session_id, body.message)


@router.post("/actions/{action_id}/discard")
def discard_alias(action_id: str, body: Owner):
    return aliases.discard_action(body.session_id, action_id)
