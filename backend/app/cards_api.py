from fastapi import APIRouter, Query
from pydantic import BaseModel, ConfigDict, Field
from typing import Literal
from . import cards

router=APIRouter(prefix='/api/cards',tags=['Simulated cards'])

class Owner(BaseModel):
    model_config=ConfigDict(extra='forbid')
    session_id:str=Field(min_length=1,max_length=100)
class Update(Owner):
    operation:Literal['lock','unlock','online_off','online_on','report_loss','payment_limit','credit_increase']
    amount_yuan:str|None=Field(default=None,max_length=30)
class Apply(Owner):
    product_id:Literal['debit','credit']
    applicant:str=Field(min_length=1,max_length=60)
    income_yuan:str=Field(min_length=1,max_length=30)
class Payment(Owner):
    amount_yuan:str=Field(min_length=1,max_length=30)
    channel:Literal['online','pos']
    merchant:str=Field(min_length=1,max_length=60)
class Interpret(Owner):
    message:str=Field(min_length=1,max_length=500)

@router.get('')
def snapshot(session_id:str=Query(min_length=1,max_length=100)):
    return cards.snapshot(session_id)
@router.post('/interpret')
def interpret(r:Interpret):
    return cards.interpret(r.session_id,r.message)
@router.post('/applications/prepare')
def apply(r:Apply):
    return cards.prepare_application(r.session_id,r.product_id,r.applicant,r.income_yuan)
@router.post('/applications/{id}/simulate-review')
def review(id:str,r:Owner):
    return cards.review_application(r.session_id,id)
@router.post('/applications/{id}/simulate-issue')
def issue(id:str,r:Owner):
    return cards.issue_card(r.session_id,id)
@router.post('/{id}/prepare')
def update(id:str,r:Update):
    return cards.prepare_update(r.session_id,id,r.operation,r.amount_yuan)
@router.post('/{id}/payment/prepare')
def payment(id:str,r:Payment):
    return cards.prepare_payment(r.session_id,id,r.amount_yuan,r.channel,r.merchant)
