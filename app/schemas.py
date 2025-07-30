from pydantic import BaseModel, Field
from typing import Optional, Literal

class TransferRequest(BaseModel):
    from_account: str
    to_account: str
    amount_minor: int
    currency: Literal["DOP"] 

class TransferResponse(BaseModel):
    transfer_id: str
    status: Literal["POSTED","FAILED"]

class CreatePIRequest(BaseModel):
    account_no: str
    amount_minor: int
    currency: str = "DOP"
    description: Optional[str] = None
    create_link: bool = True
    link_kind: Literal["URL","QR"] = "URL"

class PIResponse(BaseModel):
    id: str
    status: str
    paylink: Optional[dict] = None

class ConfirmRequest(BaseModel):
    card_number: str
    exp_month: int
    exp_year: int
    cvc: str

class QRRequest(BaseModel):
    account_no: str
    method: Literal["URL","QR"] = "URL"
    payment_intent_id: Optional[str] = None
