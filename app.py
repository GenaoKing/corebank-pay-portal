"""
CoreBank – Portal de links de pago
Requisitos: FastAPI, SQLAlchemy 2, Uvicorn, Gunicorn, Pydantic, PyMySQL
"""

import os
import secrets
from datetime import datetime, timedelta
from decimal import Decimal, ROUND_HALF_UP
from uuid import uuid4

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from pydantic import BaseModel, PositiveFloat, constr
from sqlalchemy import (Boolean, CheckConstraint, Column, DateTime, Enum, ForeignKey,
                        Numeric, String, create_engine, select)
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session, declarative_base, mapped_column

# ---------------------------------------------------------------------------
#  Configuración de base de datos
# ---------------------------------------------------------------------------

DB_URL = os.getenv(
    "DATABASE_URL",
    "sqlite:///./dev.db"
)  # Ejemplo Azure: mysql+pymysql://user:pass@server.mysql.database.azure.com/db

connect_args = {}
if DB_URL.startswith("sqlite"):
    connect_args = {"check_same_thread": False}
elif DB_URL.startswith("mysql+pymysql"):
    # TLS usando el CA de la imagen de App Service
    connect_args = {"ssl": {"ca": "/etc/ssl/certs/ca-certificates.crt"}}

engine = create_engine(
    DB_URL,
    future=True,
    echo=False,
    connect_args=connect_args,
    pool_pre_ping=True,
    pool_recycle=300,
)

SessionLocal = Session(bind=engine, autoflush=False, future=True)
Base = declarative_base()

# ---------------------------------------------------------------------------
#  Modelos
# ---------------------------------------------------------------------------


class AccountType(str, Enum):
    SAVINGS = "savings"
    CREDIT = "credit"


class Account(Base):
    __tablename__ = "accounts"

    id: str = mapped_column(String(64), primary_key=True)
    type: AccountType = mapped_column(Enum(AccountType))
    balance: Decimal = mapped_column(Numeric(18, 2), default=Decimal("0.00"))
    credit_limit: Decimal = mapped_column(Numeric(18, 2), default=Decimal("0.00"))
    available_credit: Decimal = mapped_column(Numeric(18, 2), default=Decimal("0.00"))


class PaymentIntentStatus(str, Enum):
    REQUIRES_PAYMENT = "REQUIRES_PAYMENT"
    CAPTURED = "CAPTURED"


class PaymentIntent(Base):
    __tablename__ = "payment_intents"

    id: str = mapped_column(String(36), primary_key=True)
    dest_account_id: str = mapped_column(ForeignKey("accounts.id"))
    amount: Decimal = mapped_column(Numeric(18, 2))
    currency: str = mapped_column(String(3), default="DOP")
    status: PaymentIntentStatus = mapped_column(Enum(PaymentIntentStatus))
    created_at: datetime = mapped_column(DateTime, default=datetime.utcnow)


class Paylink(Base):
    __tablename__ = "paylinks"

    slug: str = mapped_column(String(32), primary_key=True)
    payment_intent_id: str = mapped_column(ForeignKey("payment_intents.id"))
    expires_at: datetime = mapped_column(DateTime)
    used: bool = mapped_column(Boolean, default=False)
    created_at: datetime = mapped_column(DateTime, default=datetime.utcnow)


# ---------------------------------------------------------------------------
#  App FastAPI
# ---------------------------------------------------------------------------

app = FastAPI(title="CoreBank Pay-Portal")


@app.on_event("startup")
def _create_schema_if_needed():
    try:
        Base.metadata.create_all(engine)
    except OperationalError as exc:
        # No tumbamos la app si la DB no está lista (útil al arrancar en Azure).
        import traceback

        traceback.print_exc()


# ----------------------------- Utilidades ----------------------------------


def money(value: float | Decimal) -> Decimal:
    return Decimal(value).quantize(Decimal("0.01"), ROUND_HALF_UP)


def get_account(session: Session, acc_id: str) -> Account:
    obj = session.get(Account, acc_id)
    if not obj:
        raise HTTPException(404, f"Cuenta no encontrada: {acc_id}")
    return obj


# ---------------------------------------------------------------------------
#  Schemas Pydantic
# ---------------------------------------------------------------------------

AccountId = constr(min_length=1, max_length=64, strip_whitespace=True)


class LinkRequest(BaseModel):
    cuenta_destino: AccountId
    monto: PositiveFloat


class TransferRequest(BaseModel):
    cuenta_origen: AccountId
    cuenta_destino: AccountId
    monto: PositiveFloat


# ---------------------------------------------------------------------------
#  Endpoints públicos
# ---------------------------------------------------------------------------


@app.post("/create-payment-link")
def create_payment_link(data: LinkRequest):
    """Crea PaymentIntent + Paylink y devuelve URL."""
    amount = money(data.monto)

    with SessionLocal.begin() as db:
        dest = get_account(db, data.cuenta_destino)

        # 1) PaymentIntent
        pi = PaymentIntent(
            id=str(uuid4()),
            dest_account_id=dest.id,
            amount=amount,
            status=PaymentIntentStatus.REQUIRES_PAYMENT,
        )
        db.add(pi)

        # 2) Link con slug aleatorio 32 caracteres válido 30 min
        slug = secrets.token_urlsafe(16)
        link = Paylink(
            slug=slug,
            payment_intent_id=pi.id,
            expires_at=datetime.utcnow() + timedelta(minutes=30),
        )
        db.add(link)

    return {
        "link": f"/link-de-pago/{slug}",
        "payment_intent_id": pi.id,
        "expires_at": link.expires_at.isoformat(),
    }


@app.get("/link-de-pago/{slug}", response_class=HTMLResponse)
def show_link(slug: str):
    """Página HTML con formulario de pago."""
    with SessionLocal() as db:
        link = db.get(Paylink, slug)
        if not link or link.used or link.expires_at < datetime.utcnow():
            raise HTTPException(404, "Link inválido o expirado")

        pi = db.get(PaymentIntent, link.payment_intent_id)

    html = f"""
    <!doctype html>
    <html><head><title>Link de pago</title></head>
    <body>
      <h2>Pagar {pi.amount} {pi.currency}</h2>
      <p>Cuenta destino: <strong>{pi.dest_account_id}</strong></p>
      <form action="/payment-intents/{pi.id}/confirm" method="post">
        Cuenta origen:<br>
        <input name="cuenta_origen" required><br><br>
        <button type="submit">Pagar</button>
      </form>
      <small>Válido hasta {link.expires_at:%Y-%m-%d %H:%M:%S UTC}</small>
    </body></html>
    """
    return HTMLResponse(html)


@app.post("/payment-intents/{pi_id}/confirm")
def confirm_payment_intent(
    pi_id: str,
    cuenta_origen: str = Form(...),
):
    with SessionLocal.begin() as db:
        pi = db.get(PaymentIntent, pi_id)
        if not pi:
            raise HTTPException(404, "PaymentIntent no encontrado")
        if pi.status == PaymentIntentStatus.CAPTURED:
            return {"status": "ok", "message": "Ya estaba pagado"}

        link_stmt = select(Paylink).where(Paylink.payment_intent_id == pi_id)
        link = db.scalars(link_stmt).first()
        if not link or link.used or link.expires_at < datetime.utcnow():
            raise HTTPException(400, "Link inválido o expirado")

        origin = get_account(db, cuenta_origen)
        dest = get_account(db, pi.dest_account_id)
        amount = pi.amount

        # Reglas de débito / crédito muy simplificadas
        if origin.type == AccountType.SAVINGS:
            if origin.balance < amount:
                raise HTTPException(400, "Fondos insuficientes")
            origin.balance -= amount
        else:  # credit
            if origin.available_credit < amount:
                raise HTTPException(400, "Crédito insuficiente")
            origin.available_credit -= amount

        if dest.type == AccountType.SAVINGS:
            dest.balance += amount
        else:
            dest.available_credit = min(
                dest.credit_limit, dest.available_credit + amount
            )

        pi.status = PaymentIntentStatus.CAPTURED
        link.used = True

    # Redirige a página de éxito
    return RedirectResponse(url=f"/exito/{pi.id}", status_code=303)


@app.get("/exito/{pi_id}", response_class=HTMLResponse)
def payment_success(pi_id: str):
    return HTMLResponse(
        f"<h3>¡Pago realizado!</h3><p>ID de transacción: {pi_id}</p>"
    )


@app.post("/transfer")
def transfer(data: TransferRequest):
    """Endpoint usado por la app móvil/portal (fuera del flujo de links)."""
    amount = money(data.monto)

    with SessionLocal.begin() as db:
        o = get_account(db, data.cuenta_origen)
        d = get_account(db, data.cuenta_destino)

        if o.id == d.id:
            raise HTTPException(400, "Cuentas iguales")

        if o.type == AccountType.SAVINGS:
            if o.balance < amount:
                raise HTTPException(400, "Fondos insuficientes")
            o.balance -= amount
        else:
            if o.available_credit < amount:
                raise HTTPException(400, "Crédito insuficiente")
            o.available_credit -= amount

        if d.type == AccountType.SAVINGS:
            d.balance += amount
        else:
            d.available_credit = min(d.credit_limit, d.available_credit + amount)

    return {"status": "ok"}


@app.get("/health")
def health():
    return {"status": "ok"}

