import os, pymysql
from dotenv import load_dotenv

load_dotenv()

DB_CFG = {
    "host": os.environ["DB_HOST"],
    "user": os.environ["DB_USER"],
    "password": os.environ["DB_PASSWORD"],
    "database": os.environ["DB_NAME"],
    "charset": "utf8mb4",
    "cursorclass": pymysql.cursors.DictCursor,
    "ssl": {"ca": os.environ.get("DB_SSL_CA")} if os.environ.get("DB_SSL_CA") else None,
    "autocommit": False,  # usaremos transacciones manuales
}

def get_conn():
    return pymysql.connect(**DB_CFG)

# Helpers comunes
def fetch_account_by_no(conn, account_no: str):
    with conn.cursor() as cur:
        cur.execute("SELECT * FROM account WHERE account_no=%s", (account_no,))
        return cur.fetchone()

def fetch_account_by_id(conn, account_id: str):
    with conn.cursor() as cur:
        cur.execute("SELECT * FROM account WHERE id=%s", (account_id,))
        return cur.fetchone()
