import os
from uuid import uuid4
from datetime import datetime

from fastapi import FastAPI, HTTPException, Depends, Query
from fastapi.responses import HTMLResponse
from sqlalchemy import (
    create_engine, Column, String, CHAR, DateTime, BigInteger,ForeignKey,Integer 
)
from sqlalchemy.orm import sessionmaker, declarative_base, Session, relationship
from dotenv import load_dotenv
from fastapi import Request
from pydantic import BaseModel
from pydantic import BaseModel, constr, Field
from typing import Literal, Optional
import qrcode,io,base64

if os.path.exists(".env"):
    load_dotenv()

# ─── Configuration ────────────────────────────────────────────────────────────
DB_HOST    = os.getenv("DB_HOST")
DB_NAME    = os.getenv("DB_NAME")
DB_USER    = os.getenv("DB_USER")
DB_PASS    = os.getenv("DB_PASSWORD")
DB_SSL_CA  = os.getenv("DB_SSL_CA")

DATABASE_URL = (
    f"mysql+pymysql://{DB_USER}:{DB_PASS}"
    f"@{DB_HOST}/{DB_NAME}?charset=utf8mb4"
)

engine = create_engine(
    DATABASE_URL,
    connect_args={"ssl": {"ca": DB_SSL_CA}}
)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()

# ─── Models ────────────────────────────────────────────────────────────────────


class Transaction(Base):
    __tablename__ = "transaction_"

    id              = Column(CHAR(36), primary_key=True)
    created_at      = Column(DateTime, nullable=False, default=datetime.utcnow)
    type            = Column(String(32), nullable=False)    # 'TRANSFER','CARD_PAYMENT',...
    currency        = Column(CHAR(3), nullable=False)
    amount_minor    = Column(BigInteger, nullable=False)
    from_account_id = Column(CHAR(36), nullable=True)
    to_account_id   = Column(CHAR(36), nullable=True)
    status          = Column(String(16), nullable=False)    # 'POSTED','FAILED',...
    ref_external    = Column(String(64), nullable=True)
    message         = Column(String(255), nullable=True)


class Card(Base):
    __tablename__ = "card"

    id          = Column(CHAR(36), primary_key=True)
    party_id    = Column(CHAR(36), nullable=False)
    account_id  = Column(CHAR(36), nullable=False)
    brand       = Column(String(16), nullable=False)
    pan         = Column(String(32), nullable=False)
    pan_last4   = Column(CHAR(4), nullable=False)
    exp_month   = Column(Integer, nullable=False)
    exp_year    = Column(Integer, nullable=False)
    status      = Column(String(16), nullable=False, default="ACTIVE")
    created_at  = Column(DateTime, default=datetime.utcnow)


class Account(Base):
    __tablename__ = "account"
    id            = Column(CHAR(36), primary_key=True)
    party_id      = Column(CHAR(36), ForeignKey("party.id"), nullable=False)
    account_no    = Column(String(32), unique=True, nullable=False)
    currency      = Column(CHAR(3), nullable=False)
    status        = Column(String(16), nullable=False, default="ACTIVE")
    balance_minor = Column(BigInteger, nullable=False, default=0)
    created_at    = Column(DateTime, default=datetime.utcnow)

    party = relationship("Party", back_populates="accounts")


class PaymentIntent(Base):
    __tablename__ = 'payment_intent'
    id            = Column(CHAR(36), primary_key=True)
    account_id    = Column(CHAR(36), nullable=False)
    amount_minor  = Column(BigInteger, nullable=False)
    currency      = Column(CHAR(3), nullable=False, default="DOP")
    status        = Column(String(32), nullable=False, default="REQUIRES_PAYMENT")
    description   = Column(String(255))
    created_at    = Column(DateTime, default=datetime.utcnow)
    updated_at    = Column(DateTime, onupdate=datetime.utcnow)


class Party(Base):
    __tablename__ = "party"
    id         = Column(CHAR(36), primary_key=True)
    full_name  = Column(String(160), nullable=False)
    # … el resto de columnas que tengas …
    accounts   = relationship("Account", back_populates="party")


# --- Schemas Pydantic ---
class PaymentIntentCreate(BaseModel):
    account_no: constr(min_length=1, max_length=32)
    amount_minor: int = Field(gt=0)
    description: Optional[str]

class PaymentIntentOut(BaseModel):
    id: str
    status: str

class ConfirmPayment(BaseModel):
    card_number: constr(min_length=12, max_length=19)
    exp_month: int
    exp_year: int


class Paylink(Base):
    __tablename__ = "paylink"
    id                = Column(CHAR(36), primary_key=True)
    account_id        = Column(CHAR(36), nullable=False)
    payment_intent_id = Column(CHAR(36), nullable=True)
    kind              = Column(String(8), nullable=False)    # "URL" or "QR"
    slug              = Column(String(64), unique=True, nullable=False)
    expires_at        = Column(DateTime, nullable=True)
    created_at        = Column(DateTime, default=datetime.utcnow)

# ─── App & Dependencies ───────────────────────────────────────────────────────
app = FastAPI(title="Portal de Pago")

def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


# --- Schema de salida ---
class PaymentLinkResponse(BaseModel):
    slug: str
    url: str

@app.get("/health")
def health():
    # chequeo mínimo: la app está viva
    return {"status": "ok"}


@app.post("/payment-intents", response_model=PaymentIntentOut, status_code=201)
def create_payment_intent(data: PaymentIntentCreate, db: Session = Depends(get_db)):
    acct = db.query(Account).filter_by(account_no=data.account_no).first()
    if not acct:
        raise HTTPException(404, "Cuenta no encontrada")
    currency = acct.currency
    pi = PaymentIntent(
        id=str(uuid4()),
        account_id=acct.id,
        amount_minor=data.amount_minor,
        currency=currency,
        status="REQUIRES_PAYMENT",
        description=data.description
    )
    db.add(pi); db.commit()
    return {"id": pi.id, "status": pi.status}

# --- Endpoint: confirmar cobro con tarjeta ---
@app.post("/payment-intents/{pi_id}/confirm", response_model=PaymentIntentOut)
def confirm_payment(pi_id: str, data: ConfirmPayment, db: Session = Depends(get_db)):
    pi = db.query(PaymentIntent).get(pi_id)
    if not pi:
        raise HTTPException(404, "Intentión no encontrada")
    if pi.status != "REQUIRES_PAYMENT":
        return {"id": pi.id, "status": pi.status}

    # 1) Validar tarjeta (ej. tabla Card)
    card = db.query(Card).filter_by(
        pan=data.card_number,
        exp_month=data.exp_month,
        exp_year=data.exp_year,
        status="ACTIVE"
    ).first()
    if not card:
        pi.status = "FAILED"; db.commit()
        raise HTTPException(422, "Tarjeta declinada")

    # 2) Debitar y acreditar atómicamente
    from_account = db.query(Account).get(card.account_id)
    to_account   = db.query(Account).get(pi.account_id)
    if from_account.balance_minor < pi.amount_minor:
        pi.status = "FAILED"; db.commit()
        raise HTTPException(422, "Fondos insuficientes")

    # 3) Ajustar saldos y crear transacción
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
        created_at=datetime.utcnow()
    )
    pi.status = "CAPTURED"
    db.add(tx)
    db.commit()

    return {"id": pi.id, "status": pi.status}


# --- Endpoint: crear y devolver link de pago para una cuenta ---
@app.post(
    "/accounts/{account_no}/payment-link",
    response_model=PaymentLinkResponse,
    status_code=201
)
def create_payment_link(
    account_no: str,
    request: Request,
    db: Session = Depends(get_db)
):
    # 1) Verificar que la cuenta existe
    cuenta = db.query(Account).filter_by(account_no=account_no).first()
    if not cuenta:
        raise HTTPException(status_code=404, detail="Cuenta no encontrada")

    # 2) Generar slug y crear Paylink
    slug = "pl-" + uuid4().hex[:8]
    link = Paylink(
        id=str(uuid4()),
        account_id=cuenta.id,
        payment_intent_id=None,
        kind="URL",
        slug=slug,
        created_at=datetime.utcnow()
    )
    db.add(link)
    db.commit()

    # 3) Construir URL absoluta al portal de pago
    #    request.url_for('link_de_pago') ➔ http://host:port/link-de-pago
    base = request.url_for("link_de_pago")
    url  = f"{base}?code={slug}"

    return PaymentLinkResponse(slug=slug, url=url)

# ─── Endpoint: Portal de Pago ─────────────────────────────────────────────────
@app.get("/link-de-pago", response_class=HTMLResponse)
def link_de_pago(
    code: str = Query(..., description="Código corto del enlace de pago"),
    db: Session = Depends(get_db)
):
    link = db.query(Paylink).filter_by(slug=code).first()
    if not link:
        raise HTTPException(404, "Link de pago no válido")

    url_pago = f"/link-de-pago?code={code}"
    img = qrcode.make(url_pago)
    buf = io.BytesIO(); img.save(buf, format="PNG")
    qr_base64 = base64.b64encode(buf.getvalue()).decode()

    cuenta = db.query(Account).get(link.account_id)
    if not cuenta:
        raise HTTPException(404, "Cuenta destino no encontrada")
    nombre = cuenta.party.full_name
    html = f"""
    <!DOCTYPE html>
    <html>
      <head>
        <meta charset="utf-8"/>
        <title>Pagar a {cuenta.account_no}</title>
        <!-- Bootstrap CDN para estilo rápido -->
        <link 
          href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.0/dist/css/bootstrap.min.css" 
          rel="stylesheet"
        >
      </head>
      <body class="bg-light">
        <div class="container py-5">
          <div class="card mx-auto" style="max-width: 500px;">
            <div class="card-body">
              <h4 class="card-title mb-3">Pago a: {nombre}</h4>
              <p class="text-muted">Cuenta destino: <strong>{cuenta.account_no}</strong></p>
              <form id="payForm">
                <div class="mb-3">
                  <label class="form-label">{cuenta.currency}</label>
                  <input 
                    type="number" id="amount" class="form-control"
                    min="0.01" step="0.01" placeholder="Ingresa monto" required
                  />
                </div>
                <div class="mb-3">
                  <label class="form-label">Número de tarjeta</label>
                  <input 
                    type="text" id="card" class="form-control"
                    placeholder="Ej. 4111 1111 1111 1111" required
                  />
                </div>
                <div class="row g-2 mb-3">
                  <div class="col">
                    <label class="form-label">Mes</label>
                    <input type="text" id="mm" class="form-control" placeholder="MM" required>
                  </div>
                  <div class="col">
                    <label class="form-label">Año</label>
                    <input type="text" id="yy" class="form-control" placeholder="AAAA" required>
                  </div>
                </div>
                <button class="btn btn-primary w-100" type="submit">Pagar</button>
              </form>
              <pre id="resultado" class="mt-3 small"></pre>
            </div>
          </div>
        </div>
        <script>
        document.getElementById('payForm').addEventListener('submit', async e => {{
          e.preventDefault();
          const amt = Math.round(parseFloat(document.getElementById('amount').value) * 100);
          const pi = await fetch('/payment-intents', {{
            method:'POST', headers:{{'Content-Type':'application/json'}},
            body:JSON.stringify({{
              account_no: "{cuenta.account_no}",
              amount_minor: amt,
              currency: "DOP",
              description: "Pago via link {code}",
              create_link: false
            }})
          }}).then(r=>r.json());
          if (!pi.id) return document.getElementById('resultado').innerText = JSON.stringify(pi, null,2);
          const res = await fetch(`/payment-intents/${{pi.id}}/confirm`, {{
            method:'POST', headers:{{'Content-Type':'application/json'}},
            body:JSON.stringify({{
              card_number: document.getElementById('card').value,
              exp_month: parseInt(document.getElementById('mm').value),
              exp_year: parseInt(document.getElementById('yy').value),
            }})
          }}).then(r=>r.json());
          document.getElementById('resultado').innerText = JSON.stringify(res, null,2);
        }});
        </script>
        <h4>Escanear el codigo QR:</h4>
         <img src="data:image/png;base64,{qr_base64}" alt="QR de pago" width="220" height="220">
      </body>
    </html>
    """
    return HTMLResponse(html)


# ─── (Aquí podrías incluir más endpoints: health-check, listado de links, etc.) ──
