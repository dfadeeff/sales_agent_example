"""
MCP server — 4 tools for the Marble vinyl store agent.

Can be run standalone:  python mcp_server/server.py
Or imported in-process: from mcp_server.server import read_sql, create_purchase_request, ...
"""
import json
import sqlite3
import hashlib
from pathlib import Path
from typing import Any

DB_PATH = str(Path(__file__).parent.parent / "Chinook_Sqlite.sqlite")

# ---------------------------------------------------------------------------
# Internal DB helpers (sync — called from async handlers via run_in_executor
# or directly since aiosqlite is used in the API layer; MCP tools use sync
# sqlite3 to stay dependency-free when run standalone)
# ---------------------------------------------------------------------------

def _get_conn() -> sqlite3.Connection:
    """Read-write connection for purchase tools."""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def _get_readonly_conn() -> sqlite3.Connection:
    """Read-only connection (OS-level) used exclusively by read_sql.
    SQLite will raise OperationalError on any write attempt regardless of
    what SQL the caller sends — defence layer 2 after the token allowlist."""
    conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _rows_to_dicts(rows: list[sqlite3.Row]) -> list[dict]:
    return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# Tool 1: read_sql
# ---------------------------------------------------------------------------

_ALLOWED_PREFIXES = ("select", "with", "explain")


def read_sql(query: str) -> list[dict] | dict:
    """
    Execute a read-only SELECT against the Chinook database.
    Only SELECT or WITH (CTE) are permitted.
    Returns rows as a list of dicts.
    Use LIMIT to avoid giant result sets.
    """
    stripped = query.strip().lower()
    first_token = stripped.split()[0] if stripped else ""
    if first_token not in _ALLOWED_PREFIXES:
        return {"error": "Only SELECT queries are permitted. No DML or DDL allowed."}

    # Block access to app tables (passwords, tokens, etc.)
    lower = query.lower()
    for blocked in ("app_users", "messages"):
        if blocked in lower:
            return {"error": f"Access to '{blocked}' table is not permitted."}

    try:
        conn = _get_readonly_conn()
        cur = conn.execute(query)
        rows = cur.fetchall()
        conn.close()
        return _rows_to_dicts(rows)
    except sqlite3.Error as e:
        return {"error": f"SQL error: {e}"}


# ---------------------------------------------------------------------------
# Tool 2: create_purchase_request
# ---------------------------------------------------------------------------

def _idempotency_key(customer_id: int, track_ids: list[int]) -> str:
    payload = f"{customer_id}:{json.dumps(sorted(track_ids))}"
    return hashlib.sha256(payload.encode()).hexdigest()


def create_purchase_request(
    customer_id: int,
    track_ids: list[int],
) -> dict:
    """
    Create a pending purchase request for the given customer and tracks.
    Validates all TrackIds, computes total server-side from Track.UnitPrice.
    Returns purchase_request_id, total_usd, status, and line_items.
    """
    if not track_ids:
        return {"error": "track_ids must not be empty"}

    conn = _get_conn()
    try:
        placeholders = ",".join("?" * len(track_ids))
        rows = conn.execute(
            f"""SELECT t.TrackId, t.Name, t.UnitPrice,
                       al.Title as album, ar.Name as artist
                FROM Track t
                JOIN Album al ON t.AlbumId = al.AlbumId
                JOIN Artist ar ON al.ArtistId = ar.ArtistId
                WHERE t.TrackId IN ({placeholders})""",
            track_ids,
        ).fetchall()

        found_ids = {r["TrackId"] for r in rows}
        missing = sorted(set(track_ids) - found_ids)
        if missing:
            return {"error": f"TrackIds not found in catalog: {missing}"}

        total_usd = round(sum(r["UnitPrice"] for r in rows), 2)
        ikey = _idempotency_key(customer_id, track_ids)
        track_ids_json = json.dumps(sorted(track_ids))

        # Check for existing non-terminal request with same idempotency key
        existing = conn.execute(
            "SELECT purchase_request_id, status, total_usd "
            "FROM purchase_requests WHERE idempotency_key = ?",
            (ikey,),
        ).fetchone()

        if existing and existing["status"] in ("pending", "approved"):
            return {
                "purchase_request_id": existing["purchase_request_id"],
                "status": existing["status"],
                "total_usd": existing["total_usd"],
                "line_items": _rows_to_dicts(rows),
                "note": "Existing active request returned (idempotent).",
            }

        cur = conn.execute(
            """INSERT INTO purchase_requests
               (customer_id, track_ids_json, total_usd, idempotency_key)
               VALUES (?, ?, ?, ?)""",
            (customer_id, track_ids_json, total_usd, ikey),
        )
        purchase_request_id = cur.lastrowid
        conn.commit()
        return {
            "purchase_request_id": purchase_request_id,
            "status": "pending",
            "total_usd": total_usd,
            "line_items": _rows_to_dicts(rows),
            "message": "Your purchase request has been submitted for review.",
        }
    except sqlite3.Error as e:
        return {"error": f"Database error: {e}"}
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Tool 3: get_purchase_status
# ---------------------------------------------------------------------------

def get_purchase_status(purchase_request_id: int) -> dict:
    """
    Check the current status of a purchase request.
    Returns: purchase_request_id, status (pending/approved/denied/completed),
             total_usd, and denial_reason if denied.
    """
    conn = _get_conn()
    try:
        row = conn.execute(
            """SELECT purchase_request_id, status, total_usd, denial_reason,
                      track_ids_json, invoice_id
               FROM purchase_requests WHERE purchase_request_id = ?""",
            (purchase_request_id,),
        ).fetchone()
        if not row:
            return {"error": f"purchase_request_id {purchase_request_id} not found"}
        result = dict(row)
        result.pop("track_ids_json", None)
        return result
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Tool 4: complete_purchase  (HITL gate lives here)
# ---------------------------------------------------------------------------

def complete_purchase(
    purchase_request_id: int,
    billing_address: str,
    billing_city: str,
    billing_state: str,
    billing_country: str,
    billing_postal_code: str,
) -> dict:
    """
    Finalise an approved purchase. Creates Invoice + InvoiceLine rows.
    Enforces status='approved'. Returns invoice_id and total.
    Only call after the customer has provided their shipping/billing address.
    """
    conn = _get_conn()
    try:
        # BEGIN IMMEDIATE acquires the write lock before we read status,
        # making the check-then-write atomic even under concurrent access.
        conn.execute("BEGIN IMMEDIATE")

        row = conn.execute(
            "SELECT purchase_request_id, customer_id, status, total_usd, "
            "track_ids_json, invoice_id "
            "FROM purchase_requests WHERE purchase_request_id = ?",
            (purchase_request_id,),
        ).fetchone()

        if row is None:
            conn.execute("ROLLBACK")
            return {"error": f"purchase_request_id {purchase_request_id} not found"}

        if row["status"] != "approved":
            conn.execute("ROLLBACK")
            return {
                "error": (
                    f"Cannot complete: status is '{row['status']}', not 'approved'. "
                    "Please wait for admin approval."
                )
            }

        if row["invoice_id"] is not None:
            conn.execute("ROLLBACK")
            return {
                "error": "already completed",
                "invoice_id": row["invoice_id"],
                "message": "This purchase has already been completed.",
            }

        # Compute total server-side from Track.UnitPrice (never trust stored total alone)
        track_ids = json.loads(row["track_ids_json"])
        placeholders = ",".join("?" * len(track_ids))
        track_rows = conn.execute(
            f"SELECT TrackId, UnitPrice FROM Track WHERE TrackId IN ({placeholders})",
            track_ids,
        ).fetchall()

        if len(track_rows) != len(track_ids):
            conn.execute("ROLLBACK")
            return {"error": "Some tracks are no longer available in the catalog."}

        total = round(sum(r["UnitPrice"] for r in track_rows), 2)

        # Insert Invoice
        inv_cur = conn.execute(
            """INSERT INTO Invoice
               (CustomerId, InvoiceDate, BillingAddress, BillingCity,
                BillingState, BillingCountry, BillingPostalCode, Total)
               VALUES (?, datetime('now'), ?, ?, ?, ?, ?, ?)""",
            (
                row["customer_id"], billing_address, billing_city,
                billing_state, billing_country, billing_postal_code, total,
            ),
        )
        invoice_id = inv_cur.lastrowid

        # Insert InvoiceLines (one per track, UnitPrice from Track table)
        for track_row in track_rows:
            conn.execute(
                "INSERT INTO InvoiceLine (InvoiceId, TrackId, UnitPrice, Quantity) "
                "VALUES (?, ?, ?, 1)",
                (invoice_id, track_row["TrackId"], track_row["UnitPrice"]),
            )

        # Transition status to 'completed' — final atomic guard.
        # cursor.rowcount == 0 means another concurrent call beat us
        # (BEGIN IMMEDIATE serialises writers, so this only fires if the first
        # call committed between our status check and this UPDATE — impossible
        # within one transaction, but belt-and-suspenders for future refactors).
        update_cur = conn.execute(
            "UPDATE purchase_requests "
            "SET status='completed', invoice_id=?, updated_at=datetime('now') "
            "WHERE purchase_request_id=? AND status='approved'",
            (invoice_id, purchase_request_id),
        )

        if update_cur.rowcount == 0:
            # Status was no longer 'approved' when we tried to commit
            conn.execute("ROLLBACK")
            existing_row = conn.execute(
                "SELECT invoice_id FROM purchase_requests WHERE purchase_request_id=?",
                (purchase_request_id,),
            ).fetchone()
            return {
                "error": "already completed",
                "invoice_id": existing_row["invoice_id"] if existing_row else None,
            }

        conn.execute("COMMIT")
        return {
            "invoice_id": invoice_id,
            "total_usd": total,
            "track_count": len(track_ids),
            "message": (
                f"Purchase complete! Invoice #{invoice_id} created for "
                f"{len(track_ids)} track(s) totalling ${total:.2f}."
            ),
        }
    except sqlite3.Error as e:
        try:
            conn.execute("ROLLBACK")
        except Exception:
            pass
        return {"error": f"Database error: {e}"}
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Async write tools — use the shared aiosqlite connection from api/db.py.
# These replace the sync versions inside the agent loop so that both the
# API layer and the MCP tools share one connection, eliminating the
# "database is locked" error caused by two writers competing.
# ---------------------------------------------------------------------------

async def async_create_purchase_request(
    db,  # aiosqlite.Connection
    customer_id: int,
    track_ids: list[int],
) -> dict:
    """Async version used by the agent loop (shared connection)."""
    if not track_ids:
        return {"error": "track_ids must not be empty"}

    try:
        placeholders = ",".join("?" * len(track_ids))
        async with db.execute(
            f"""SELECT t.TrackId, t.Name, t.UnitPrice,
                       al.Title as album, ar.Name as artist
                FROM Track t
                JOIN Album al ON t.AlbumId = al.AlbumId
                JOIN Artist ar ON al.ArtistId = ar.ArtistId
                WHERE t.TrackId IN ({placeholders})""",
            track_ids,
        ) as cur:
            rows = await cur.fetchall()

        found_ids = {r["TrackId"] for r in rows}
        missing = sorted(set(track_ids) - found_ids)
        if missing:
            return {"error": f"TrackIds not found in catalog: {missing}"}

        total_usd = round(sum(r["UnitPrice"] for r in rows), 2)
        ikey = _idempotency_key(customer_id, track_ids)
        track_ids_json = json.dumps(sorted(track_ids))

        # Check idempotency
        async with db.execute(
            "SELECT purchase_request_id, status, total_usd "
            "FROM purchase_requests WHERE idempotency_key = ?",
            (ikey,),
        ) as cur:
            existing = await cur.fetchone()

        if existing and existing["status"] in ("pending", "approved"):
            return {
                "purchase_request_id": existing["purchase_request_id"],
                "status": existing["status"],
                "total_usd": existing["total_usd"],
                "line_items": [dict(r) for r in rows],
                "note": "Existing active request returned (idempotent).",
            }

        async with db.execute(
            "INSERT INTO purchase_requests (customer_id, track_ids_json, total_usd, idempotency_key) "
            "VALUES (?, ?, ?, ?)",
            (customer_id, track_ids_json, total_usd, ikey),
        ) as cur:
            purchase_request_id = cur.lastrowid

        return {
            "purchase_request_id": purchase_request_id,
            "status": "pending",
            "total_usd": total_usd,
            "line_items": [dict(r) for r in rows],
            "message": "Your purchase request has been submitted for review.",
        }
    except Exception as e:
        return {"error": f"Database error: {e}"}


async def async_complete_purchase(
    db,  # aiosqlite.Connection
    purchase_request_id: int,
    billing_address: str,
    billing_city: str,
    billing_state: str,
    billing_country: str,
    billing_postal_code: str,
) -> dict:
    """Async version used by the agent loop (shared connection, explicit transaction)."""
    try:
        # BEGIN IMMEDIATE on the shared connection — no second connection, no lock fight
        await db.execute("BEGIN IMMEDIATE")

        async with db.execute(
            "SELECT purchase_request_id, customer_id, status, total_usd, "
            "track_ids_json, invoice_id FROM purchase_requests WHERE purchase_request_id = ?",
            (purchase_request_id,),
        ) as cur:
            row = await cur.fetchone()

        if row is None:
            await db.execute("ROLLBACK")
            return {"error": f"purchase_request_id {purchase_request_id} not found"}

        if row["status"] != "approved":
            await db.execute("ROLLBACK")
            return {"error": f"Cannot complete: status is '{row['status']}', not 'approved'."}

        if row["invoice_id"] is not None:
            await db.execute("ROLLBACK")
            return {"error": "already completed", "invoice_id": row["invoice_id"]}

        track_ids = json.loads(row["track_ids_json"])
        placeholders = ",".join("?" * len(track_ids))
        async with db.execute(
            f"SELECT TrackId, UnitPrice FROM Track WHERE TrackId IN ({placeholders})",
            track_ids,
        ) as cur:
            track_rows = await cur.fetchall()

        if len(track_rows) != len(track_ids):
            await db.execute("ROLLBACK")
            return {"error": "Some tracks are no longer available in the catalog."}

        total = round(sum(r["UnitPrice"] for r in track_rows), 2)

        async with db.execute(
            "INSERT INTO Invoice (CustomerId, InvoiceDate, BillingAddress, BillingCity, "
            "BillingState, BillingCountry, BillingPostalCode, Total) "
            "VALUES (?, datetime('now'), ?, ?, ?, ?, ?, ?)",
            (row["customer_id"], billing_address, billing_city,
             billing_state, billing_country, billing_postal_code, total),
        ) as cur:
            invoice_id = cur.lastrowid

        for track_row in track_rows:
            await db.execute(
                "INSERT INTO InvoiceLine (InvoiceId, TrackId, UnitPrice, Quantity) VALUES (?, ?, ?, 1)",
                (invoice_id, track_row["TrackId"], track_row["UnitPrice"]),
            )

        async with db.execute(
            "UPDATE purchase_requests SET status='completed', invoice_id=?, updated_at=datetime('now') "
            "WHERE purchase_request_id=? AND status='approved'",
            (invoice_id, purchase_request_id),
        ) as cur:
            if cur.rowcount == 0:
                await db.execute("ROLLBACK")
                async with db.execute(
                    "SELECT invoice_id FROM purchase_requests WHERE purchase_request_id=?",
                    (purchase_request_id,),
                ) as cur2:
                    existing = await cur2.fetchone()
                return {"error": "already completed", "invoice_id": existing["invoice_id"] if existing else None}

        await db.execute("COMMIT")
        return {
            "invoice_id": invoice_id,
            "total_usd": total,
            "track_count": len(track_ids),
            "message": f"Purchase complete! Invoice #{invoice_id} created for {len(track_ids)} track(s) totalling ${total:.2f}.",
        }
    except Exception as e:
        try:
            await db.execute("ROLLBACK")
        except Exception:
            pass
        return {"error": f"Database error: {e}"}


async def async_get_purchase_status(db, purchase_request_id: int) -> dict:
    """Async version used by the agent loop (shared connection)."""
    try:
        async with db.execute(
            "SELECT purchase_request_id, status, total_usd, denial_reason, invoice_id "
            "FROM purchase_requests WHERE purchase_request_id = ?",
            (purchase_request_id,),
        ) as cur:
            row = await cur.fetchone()
        if not row:
            return {"error": f"purchase_request_id {purchase_request_id} not found"}
        return dict(row)
    except Exception as e:
        return {"error": f"Database error: {e}"}


# ---------------------------------------------------------------------------
# FastMCP wrapper (for standalone / Claude Desktop use)
# ---------------------------------------------------------------------------

def _make_mcp_server():
    try:
        from mcp.server.fastmcp import FastMCP
        mcp = FastMCP("marble-vinyl-store")

        @mcp.tool()
        def mcp_read_sql(query: str) -> list[dict] | dict:
            """Execute a read-only SELECT against the Chinook database.
            Only SELECT or WITH (CTE) are permitted. Returns rows as list of dicts."""
            return read_sql(query)

        @mcp.tool()
        def mcp_create_purchase_request(customer_id: int, track_ids: list[int]) -> dict:
            """Create a pending purchase request. Validates TrackIds, computes total server-side."""
            return create_purchase_request(customer_id, track_ids)

        @mcp.tool()
        def mcp_get_purchase_status(purchase_request_id: int) -> dict:
            """Check status of a purchase request: pending/approved/denied/completed."""
            return get_purchase_status(purchase_request_id)

        @mcp.tool()
        def mcp_complete_purchase(
            purchase_request_id: int,
            billing_address: str,
            billing_city: str,
            billing_state: str,
            billing_country: str,
            billing_postal_code: str,
        ) -> dict:
            """Finalise an approved purchase. Creates Invoice + InvoiceLine rows."""
            return complete_purchase(
                purchase_request_id, billing_address, billing_city,
                billing_state, billing_country, billing_postal_code,
            )

        return mcp
    except ImportError:
        return None


if __name__ == "__main__":
    mcp = _make_mcp_server()
    if mcp:
        mcp.run(transport="stdio")
    else:
        print("mcp package not installed. Run: pip install mcp")
