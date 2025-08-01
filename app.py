import os, io, base64
from uuid import uuid4
from datetime import datetime
from typing import Optional

import qrcode
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Depends, Query, Request
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, constr, Field
from sqlalchemy import (
    create_engine, Column, String, CHAR, DateTime,
    BigInteger, ForeignKey, Integer
)
from sqlalchemy.orm import declarative_base, relationship, sessionmaker, Session

# ─── Config ─────────────────────────────────────────────────────────────
if os.path.exists(".env"):
    load_dotenv()

DB_HOST = os.getenv("DB_HOST")
DB_NAME = os.getenv("DB_NAME")
DB_USER = os.getenv("DB_USER")
DB_PASS = os.getenv("DB_PASSWORD")
DB_SSL_CA = os.getenv("DB_SSL_CA")

DATABASE_URL = f"mysql+pymysql://{DB_USER}:{DB_PASS}@{DB_HOST}/{DB_NAME}?charset=utf8mb4"
engine = create_engine(DATABASE_URL, connect_args={"ssl": {"ca": DB_SSL_CA}})
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()

# ─── Models ─────────────────────────────────────────────────────────────
class Transaction(Base):
    __tablename__ = "transaction_"
    id               = Column(CHAR(36), primary_key=True)
    created_at       = Column(DateTime, default=datetime.utcnow, nullable=False)
    type             = Column(String(32), nullable=False)
    currency         = Column(CHAR(3), nullable=False)
    amount_minor     = Column(BigInteger, nullable=False)
    from_account_id  = Column(CHAR(36))
    to_account_id    = Column(CHAR(36))
    status           = Column(String(16), nullable=False)
    ref_external     = Column(String(64))
    message          = Column(String(255))

class Card(Base):
    __tablename__ = "card"
    id         = Column(CHAR(36), primary_key=True)
    party_id   = Column(CHAR(36), nullable=False)
    account_id = Column(CHAR(36), nullable=False)
    brand      = Column(String(16), nullable=False)
    pan        = Column(String(32), nullable=False)
    pan_last4  = Column(CHAR(4), nullable=False)
    exp_month  = Column(Integer, nullable=False)
    exp_year   = Column(Integer, nullable=False)
    status     = Column(String(16), default="ACTIVE", nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)

class Account(Base):
    __tablename__ = "account"
    id            = Column(CHAR(36), primary_key=True)
    party_id      = Column(CHAR(36), ForeignKey("party.id"), nullable=False)
    account_no    = Column(String(32), unique=True, nullable=False)
    currency      = Column(CHAR(3), nullable=False)
    status        = Column(String(16), default="ACTIVE", nullable=False)
    balance_minor = Column(BigInteger, default=0, nullable=False)
    created_at    = Column(DateTime, default=datetime.utcnow)

    party = relationship("Party", back_populates="accounts")

class PaymentIntent(Base):
    __tablename__ = "payment_intent"
    id            = Column(CHAR(36), primary_key=True)
    account_id    = Column(CHAR(36), nullable=False)
    amount_minor  = Column(BigInteger, nullable=False)
    currency      = Column(CHAR(3), default="DOP", nullable=False)
    status        = Column(String(32), default="REQUIRES_PAYMENT", nullable=False)
    description   = Column(String(255))
    created_at    = Column(DateTime, default=datetime.utcnow)
    updated_at    = Column(DateTime, onupdate=datetime.utcnow)

class Party(Base):
    __tablename__ = "party"
    id        = Column(CHAR(36), primary_key=True)
    full_name = Column(String(160), nullable=False)
    accounts  = relationship("Account", back_populates="party")

class Paylink(Base):
    __tablename__ = "paylink"
    id               = Column(CHAR(36), primary_key=True)
    account_id       = Column(CHAR(36), nullable=False)
    payment_intent_id= Column(CHAR(36))
    kind             = Column(String(8), nullable=False)          # 'URL'
    slug             = Column(String(64), unique=True, nullable=False)
    expires_at       = Column(DateTime)
    created_at       = Column(DateTime, default=datetime.utcnow)

# ─── FastAPI ────────────────────────────────────────────────────────────
app = FastAPI(title="Portal de Pago")

def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

# ─── Schemas ────────────────────────────────────────────────────────────
class PaymentIntentCreate(BaseModel):
    account_no: constr(min_length=1, max_length=32)
    amount_minor: int = Field(gt=0)
    description: Optional[str] = None

class PaymentIntentOut(BaseModel):
    id: str
    status: str

class ConfirmPayment(BaseModel):
    card_number: constr(min_length=12, max_length=19)
    exp_month: int
    exp_year: int

class PaymentLinkResponse(BaseModel):
    slug: str
    url: str

# ─── Endpoints ──────────────────────────────────────────────────────────
@app.post("/payment-intents", response_model=PaymentIntentOut, status_code=201)
def create_payment_intent(data: PaymentIntentCreate, db: Session = Depends(get_db)):
    acct = db.query(Account).filter_by(account_no=data.account_no).first()
    if not acct:
        raise HTTPException(404, "Cuenta no encontrada")

    pi = PaymentIntent(
        id=str(uuid4()),
        account_id=acct.id,
        amount_minor=data.amount_minor,
        currency=acct.currency,
        description=data.description,
    )
    db.add(pi); db.commit()
    return {"id": pi.id, "status": pi.status}

@app.post("/payment-intents/{pi_id}/confirm", response_model=PaymentIntentOut)
def confirm_payment(pi_id: str, data: ConfirmPayment, db: Session = Depends(get_db)):
    pi = db.query(PaymentIntent).get(pi_id)
    if not pi:
        raise HTTPException(404, "Intento no encontrado")

    if pi.status != "REQUIRES_PAYMENT":
        return {"id": pi.id, "status": pi.status}

    card = (
        db.query(Card)
        .filter_by(
            pan=data.card_number,
            exp_month=data.exp_month,
            exp_year=data.exp_year,
            status="ACTIVE",
        )
        .first()
    )
    if not card:
        pi.status = "FAILED"; db.commit()
        raise HTTPException(422, "Tarjeta declinada")

    from_account = db.query(Account).get(card.account_id)
    to_account   = db.query(Account).get(pi.account_id)

    if from_account.balance_minor < pi.amount_minor:
        pi.status = "FAILED"; db.commit()
        raise HTTPException(422, "Fondos insuficientes")

    from_account.balance_minor -= pi.amount_minor
    to_account.balance_minor   += pi.amount_minor

    tx = Transaction(
        id=str(uuid4()),
        type="CARD_PAYMENT",
        currency=pi.currency,
        amount_minor=pi.amount_minor,
        from_account_id=from_account.id,
        to_account_id=to_account.id,
        status="POSTED",
    )
    db.add(tx); pi.status = "CAPTURED"; db.commit()
    return {"id": pi.id, "status": pi.status}

@app.post(
    "/accounts/{account_no}/payment-link",
    response_model=PaymentLinkResponse,
    status_code=201,
)
def create_payment_link(account_no: str, request: Request, db: Session = Depends(get_db)):
    cuenta = db.query(Account).filter_by(account_no=account_no).first()
    if not cuenta:
        raise HTTPException(404, "Cuenta no encontrada")

    slug = f"pl-{uuid4().hex[:8]}"
    link = Paylink(id=str(uuid4()), account_id=cuenta.id, kind="URL", slug=slug)
    db.add(link); db.commit()

    url_pago = f"{request.url_for('link_de_pago')}?code={slug}"
    return PaymentLinkResponse(slug=slug, url=url_pago)

@app.get("/link-de-pago", response_class=HTMLResponse)
def link_de_pago(
    code: str = Query(..., description="Código corto del enlace de pago"),
    db: Session = Depends(get_db),
):
    link = db.query(Paylink).filter_by(slug=code).first()
    if not link:
        raise HTTPException(404, "Link de pago no válido")

    cuenta = db.query(Account).get(link.account_id)
    if not cuenta:
        raise HTTPException(404, "Cuenta destino no encontrada")

    # —— Generar QR como data-URI ——————————————————————————
    url_pago = f"/link-de-pago?code={code}"
    img = qrcode.make(url_pago)
    buf = io.BytesIO(); img.save(buf, format="PNG")
    qr_base64 = base64.b64encode(buf.getvalue()).decode()

    nombre = cuenta.party.full_name if cuenta.party else "Titular"

    html = f"""
    <h3>Pagar a {cuenta.account_no}</h3>
    <p>Pago a: {nombre}</p>

    <!--  FORMULARIO  -->
    <form action='/link-de-pago' method='get'>
      <input type='hidden' name='code' value='{code}'>
      <label>Número de tarjeta</label><br>
      <input type='text' name='card_number'><br>
      <label>Mes</label><br>
      <input type='text' name='exp_month'><br>
      <label>Año</label><br>
      <input type='text' name='exp_year'><br><br>
      <button type='submit'>Pagar</button>
    </form>

    <!--  QR justo debajo  -->
    <h4>También puedes escanear el QR:</h4>
    <img src="data:image/png;base64,{qr_base64}" alt="QR de pago" width="220" height="220">
    """
    return HTMLResponse(html)

