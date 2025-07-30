from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse
from .db import get_conn
from .schemas import *
import uuid, datetime, base64, io, qrcode

app = FastAPI(title="Core Bancario Lab")

@app.on_event("startup")
def startup():
    pass

def now():
    return datetime.datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")




@app.get("/accounts/{account_no}")
def get_account(account_no: str):
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM accounts WHERE account_no=?", (account_no,)).fetchone()
        if not row:
            raise HTTPException(404, "account not found")
        return dict(row)
    


@app.get("/accounts/{account_no}/transactions")
def get_account_transactions(account_no: str,
                             type: Optional[str] = None,
                             status: Optional[str] = None,
                             from_date: Optional[str] = None,
                             to_date: Optional[str] = None):
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT id FROM account WHERE account_no = %s", (account_no,))
            acc = cur.fetchone()
            if not acc:
                raise HTTPException(404, "Account not found")
            account_id = acc["id"]

            query = '''
                SELECT id, created_at, type, currency, amount_minor,
                       from_account_id, to_account_id, status,
                       CASE
                           WHEN from_account_id = %s THEN -amount_minor
                           WHEN to_account_id = %s THEN amount_minor
                           ELSE 0
                       END AS signed_amount
                FROM transaction_
                WHERE (from_account_id = %s OR to_account_id = %s)
            '''
            params = [account_id, account_id, account_id, account_id]

            if type:
                query += " AND type = %s"
                params.append(type)
            if status:
                query += " AND status = %s"
                params.append(status)
            if from_date:
                query += " AND created_at >= %s"
                params.append(from_date)
            if to_date:
                query += " AND created_at <= %s"
                params.append(to_date)

            query += " ORDER BY created_at DESC"
            cur.execute(query, tuple(params))
            return cur.fetchall()
    finally:
        conn.close()


@app.get("/parties")
def list_parties(doc_type: Optional[str] = None, doc_number: Optional[str] = None, name: Optional[str] = None):
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            query = "SELECT id, doc_type, doc_number, full_name, email FROM party WHERE 1=1"
            params = []
            if doc_type:
                query += " AND doc_type = %s"
                params.append(doc_type)
            if doc_number:
                query += " AND doc_number = %s"
                params.append(doc_number)
            if name:
                query += " AND full_name LIKE %s"
                params.append(f"%{name}%")
            cur.execute(query, tuple(params))
            return cur.fetchall()
    finally:
        conn.close()


@app.get("/accounts")
def list_accounts(currency: Optional[str] = None, status: Optional[str] = None):
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            query = '''
                SELECT a.account_no, p.full_name AS owner_name, a.currency, a.status, a.balance_minor
                FROM account a JOIN party p ON a.party_id = p.id WHERE 1=1
            '''
            params = []
            if currency:
                query += " AND a.currency = %s"
                params.append(currency)
            if status:
                query += " AND a.status = %s"
                params.append(status)
            cur.execute(query, tuple(params))
            return cur.fetchall()
    finally:
        conn.close()



@app.post("/transfers", response_model=TransferResponse)
def transfer(req: TransferRequest):
    tx_id = str(uuid.uuid4())
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            # Obtener datos de cuenta origen
            cur.execute("SELECT id, balance_minor, currency FROM account WHERE account_no = %s", (req.from_account,))
            from_row = cur.fetchone()

            # Obtener datos de cuenta destino
            cur.execute("SELECT id, balance_minor, currency FROM account WHERE account_no = %s", (req.to_account,))
            to_row = cur.fetchone()

            if not from_row or not to_row:
                raise HTTPException(404, "account not found")
            if from_row["currency"] != req.currency or to_row["currency"] != req.currency:
                raise HTTPException(422, "currency mismatch")
            if from_row["balance_minor"] < req.amount_minor:
                raise HTTPException(422, "INSUFFICIENT_FUNDS")

            # Aplica movimiento en una transacción
            # 1. Debitar
            cur.execute("UPDATE account SET balance_minor = balance_minor - %s WHERE id = %s",
                        (req.amount_minor, from_row["id"]))
            # 2. Acreditar
            cur.execute("UPDATE account SET balance_minor = balance_minor + %s WHERE id = %s",
                        (req.amount_minor, to_row["id"]))

            # 3. Registrar en transaction_
            cur.execute("""
                INSERT INTO transaction_ (id, created_at, type, currency, amount_minor,
                                          from_account_id, to_account_id, status, ref_external, message)
                VALUES (%s, %s, 'TRANSFER', %s, %s, %s, %s, 'POSTED', NULL, NULL)
            """, (tx_id, now(), req.currency, req.amount_minor, from_row["id"], to_row["id"]))
        
        conn.commit()
        return {"transfer_id": tx_id, "status": "POSTED"}

    except Exception as e:
        conn.rollback()
        raise HTTPException(500, f"Internal error: {str(e)}")

    finally:
        conn.close()

@app.post("/payment-intents", response_model=PIResponse, status_code=201)
def create_pi(req: CreatePIRequest):
    pi_id = str(uuid.uuid4())
    with get_conn() as conn:
        cur = conn.cursor()
        # valida account
        acc = cur.execute("SELECT account_no FROM accounts WHERE account_no=?", (req.account_no,)).fetchone()
        if not acc:
            raise HTTPException(404, "account not found")

        cur.execute("""
            INSERT INTO payment_intents (id, account_no, amount_minor, currency, status, description, created_at)
            VALUES (?, ?, ?, ?, 'REQUIRES_PAYMENT', ?, ?)
        """, (pi_id, req.account_no, req.amount_minor, req.currency, req.description, now()))

        resp = {"id": pi_id, "status": "REQUIRES_PAYMENT"}

        if req.create_link:
            slug = "pl-" + uuid.uuid4().hex[:8]
            cur.execute("""
                INSERT INTO paylinks (id, account_no, payment_intent_id, kind, slug, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
            """, (str(uuid.uuid4()), req.account_no, pi_id, req.link_kind, slug, now()))
            base_url = "https://proyectoFinalWebApp.azurewebsites.net/pay/"
            url = base_url + slug
            paylink = {"slug": slug, "url": url}

            if req.link_kind == "QR":
                img = qrcode.make(url)
                buf = io.BytesIO()
                img.save(buf, format="PNG")
                b64 = base64.b64encode(buf.getvalue()).decode("utf-8")
                paylink["data_url"] = "data:image/png;base64," + b64

            resp["paylink"] = paylink

        conn.commit()
        return resp

@app.get("/payment-intents/{pi_id}")
def get_pi(pi_id: str):
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM payment_intents WHERE id=?", (pi_id,)).fetchone()
        if not row:
            raise HTTPException(404, "not found")
        return dict(row)

@app.post("/payment-intents/{pi_id}/confirm")
def confirm_pi(pi_id: str, req: ConfirmRequest):
    with get_conn() as conn:
        cur = conn.cursor()
        pi = cur.execute("SELECT * FROM payment_intents WHERE id=?", (pi_id,)).fetchone()
        if not pi:
            raise HTTPException(404, "pi not found")
        if pi["status"] in ("CAPTURED","CANCELED"):
            return {"status": pi["status"]}

        # tarjeta válida (match simple)
        card = cur.execute("""
            SELECT c.*, a.account_no as src_account_no
            FROM cards c
            JOIN accounts a ON a.id = c.account_id
            WHERE c.pan=? AND c.exp_month=? AND c.exp_year=? AND c.cvc=?
        """, (req.card_number, req.exp_month, req.exp_year, req.cvc)).fetchone()

        if not card:
            cur.execute("UPDATE payment_intents SET status='FAILED', updated_at=? WHERE id=?",
                        (now(), pi_id))
            conn.commit()
            raise HTTPException(422, "card declined")

        # debitar cuenta de la tarjeta y acreditar destino
        amount = pi["amount_minor"]
        src = card["src_account_no"]
        dst = pi["account_no"]

        bal = cur.execute("SELECT balance_minor FROM accounts WHERE account_no=?", (src,)).fetchone()[0]
        if bal < amount:
            cur.execute("UPDATE payment_intents SET status='FAILED', updated_at=? WHERE id=?",
                        (now(), pi_id))
            conn.commit()
            raise HTTPException(422, "insufficient funds")

        cur.execute("UPDATE accounts SET balance_minor = balance_minor - ? WHERE account_no=?", (amount, src))
        cur.execute("UPDATE accounts SET balance_minor = balance_minor + ? WHERE account_no=?", (amount, dst))

        tx_id = str(uuid.uuid4())
        cur.execute("""
            INSERT INTO transactions (id, created_at, type, currency, amount_minor,
                                      from_account_no, to_account_no, status, message)
            VALUES (?, ?, 'CARD_PAYMENT', ?, ?, ?, ?, 'POSTED', NULL)
        """, (tx_id, now(), pi["currency"], amount, src, dst))

        cur.execute("UPDATE payment_intents SET status='CAPTURED', updated_at=? WHERE id=?",
                    (now(), pi_id))

        conn.commit()
        return {"status": "CAPTURED", "transaction_id": tx_id}

@app.post("/qr")
def create_qr(req: QRRequest):
    slug = "pl-" + uuid.uuid4().hex[:8]
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO paylinks (id, account_no, payment_intent_id, kind, slug, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
        """, (str(uuid.uuid4()), req.account_no, req.payment_intent_id, req.method, slug, now()))
        conn.commit()

    base_url = "https://proyectoFinalWebApp.azurewebsites.net/pay/"
    url = base_url + slug
    resp = {"slug": slug, "url": url}

    if req.method == "QR":
        img = qrcode.make(url)
        buf = io.BytesIO(); img.save(buf, format="PNG")
        resp["data_url"] = "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode()

    return resp

# Página HTML muy simple (puedes servir desde /static o templates)
@app.get("/pay/{slug}", response_class=HTMLResponse)
def pay_page(slug: str):
    # Para el lab, una página mínima que postea a confirm con tarjeta dummy
    html = f"""
    <html><body>
      <h2>Pago – {slug}</h2>
      <form method="post" action="/demo/confirm/{slug}">
        <label>Tarjeta</label><input name="card_number" value="4111111111111111"><br>
        <label>Exp Month</label><input name="exp_month" value="12"><br>
        <label>Exp Year</label><input name="exp_year" value="2030"><br>
        <label>CVC</label><input name="cvc" value="123"><br>
        <button type="submit">Pagar</button>
      </form>
    </body></html>
    """
    return HTMLResponse(html)

# Ruta de demostración que busca el PI por slug y confirma (simple)
from fastapi import Request
@app.post("/demo/confirm/{slug}")
async def demo_confirm(slug: str, request: Request):
    form = await request.form()
    card_number = form["card_number"]; exp_month = int(form["exp_month"]); exp_year = int(form["exp_year"]); cvc = form["cvc"]
    with get_conn() as conn:
        row = conn.execute("SELECT payment_intent_id FROM paylinks WHERE slug=?", (slug,)).fetchone()
        if not row or not row["payment_intent_id"]:
            raise HTTPException(404, "link without payment_intent")
        pi_id = row["payment_intent_id"]
    # Reusa la lógica del endpoint programáticamente sería ideal; aquí llamamos funciónmente simple:
    return confirm_pi(pi_id, ConfirmRequest(card_number=card_number, exp_month=exp_month, exp_year=exp_year, cvc=cvc))
