"""
One-time setup: create app tables in Chinook_Sqlite.sqlite and seed admin accounts.
Run: python setup.py
"""
import sqlite3
from pathlib import Path

DB_PATH = Path(__file__).parent / "Chinook_Sqlite.sqlite"
ADMIN_PASSWORD = "admin123"

DDL = """
CREATE TABLE IF NOT EXISTS app_users (
    user_id       INTEGER PRIMARY KEY AUTOINCREMENT,
    customer_id   INTEGER REFERENCES Customer(CustomerId),
    email         TEXT    NOT NULL UNIQUE,
    password_hash TEXT    NOT NULL,
    role          TEXT    NOT NULL CHECK(role IN ('customer','admin')),
    created_at    TEXT    NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS conversations (
    conversation_id INTEGER PRIMARY KEY AUTOINCREMENT,
    customer_id     INTEGER NOT NULL REFERENCES Customer(CustomerId),
    created_at      TEXT    NOT NULL DEFAULT (datetime('now')),
    updated_at      TEXT    NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_conv_customer ON conversations(customer_id);

CREATE TABLE IF NOT EXISTS messages (
    message_id      INTEGER PRIMARY KEY AUTOINCREMENT,
    conversation_id INTEGER NOT NULL REFERENCES conversations(conversation_id),
    role            TEXT    NOT NULL CHECK(role IN ('user','assistant')),
    content         TEXT    NOT NULL,
    created_at      TEXT    NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_msg_conv ON messages(conversation_id, created_at);

CREATE TABLE IF NOT EXISTS purchase_requests (
    purchase_request_id INTEGER PRIMARY KEY AUTOINCREMENT,
    customer_id         INTEGER NOT NULL REFERENCES Customer(CustomerId),
    track_ids_json      TEXT    NOT NULL,
    total_usd           REAL    NOT NULL,
    status              TEXT    NOT NULL DEFAULT 'pending'
                                CHECK(status IN ('pending','approved','denied','completed')),
    denial_reason       TEXT,
    invoice_id          INTEGER REFERENCES Invoice(InvoiceId),
    idempotency_key     TEXT    UNIQUE,
    reviewed_by         INTEGER REFERENCES app_users(user_id),
    reviewed_at         TEXT,
    created_at          TEXT    NOT NULL DEFAULT (datetime('now')),
    updated_at          TEXT    NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_pr_status   ON purchase_requests(status, created_at);
CREATE INDEX IF NOT EXISTS idx_pr_customer ON purchase_requests(customer_id, status);

-- notification tracking (idempotent ALTER)
"""

DDL_MIGRATIONS = """
ALTER TABLE purchase_requests ADD COLUMN notification_sent INTEGER DEFAULT 0;
"""


def hash_password(password: str) -> str:
    from argon2 import PasswordHasher
    return PasswordHasher().hash(password)


def seed_admins(conn: sqlite3.Connection) -> None:
    cursor = conn.execute(
        "SELECT Email, FirstName, LastName FROM Employee "
        "WHERE Title LIKE '%Sales%'"
    )
    admins = cursor.fetchall()
    ph = hash_password(ADMIN_PASSWORD)
    for email, first, last in admins:
        try:
            conn.execute(
                "INSERT INTO app_users (customer_id, email, password_hash, role) "
                "VALUES (NULL, ?, ?, 'admin')",
                (email, ph),
            )
            print(f"  Seeded admin: {first} {last} <{email}>")
        except sqlite3.IntegrityError:
            print(f"  Admin already exists: {email}")
    conn.commit()


def main() -> None:
    if not DB_PATH.exists():
        print(f"ERROR: {DB_PATH} not found. Download Chinook_Sqlite.sqlite first.")
        raise SystemExit(1)

    conn = sqlite3.connect(DB_PATH)
    conn.executescript(DDL)
    conn.commit()
    print("App tables created (or already exist).")

    # Idempotent migrations
    for stmt in DDL_MIGRATIONS.strip().split(";"):
        stmt = stmt.strip()
        if not stmt:
            continue
        try:
            conn.execute(stmt)
            conn.commit()
        except sqlite3.OperationalError:
            pass  # column already exists

    seed_admins(conn)
    conn.close()
    print(f"\nSetup complete. Admin password: {ADMIN_PASSWORD}")
    print("Start server: uvicorn api.main:app --reload")


if __name__ == "__main__":
    main()
