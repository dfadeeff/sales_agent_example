#!/usr/bin/env python3
"""
UC-1 end-to-end smoke test.

Iron Maiden dual-album flow:
  Customer finds "Hallowed Be Thy Name" → browses two albums →
  buys both → admin approves → customer provides shipping →
  agent completes purchase → verify Invoice + InvoiceLines in DB.
"""
import json
import sqlite3
import sys
import time

import httpx

BASE = "http://localhost:8000"
DB = "Chinook_Sqlite.sqlite"

CUSTOMER_EMAIL = "uc1_test@example.com"
CUSTOMER_PASSWORD = "test1234"
ADMIN_EMAIL = "nancy@chinookcorp.com"
ADMIN_PASSWORD = "admin123"

PASS = "\033[92m✓\033[0m"
FAIL = "\033[91m✗\033[0m"
INFO = "\033[94m→\033[0m"


# ─────────────────────────────────────────────────────────────
# SSE helper
# ─────────────────────────────────────────────────────────────

def chat(token: str, message: str) -> str:
    """Send one chat message, print streamed text, return full text."""
    print(f"\n  {INFO} Customer: {message}")
    print(f"  {INFO} Agent: ", end="", flush=True)

    full_text = ""
    with httpx.Client(timeout=120) as client:
        with client.stream(
            "POST", f"{BASE}/chat",
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            },
            content=json.dumps({"message": message}),
        ) as resp:
            resp.raise_for_status()
            buf = ""
            for chunk in resp.iter_bytes():
                buf += chunk.decode("utf-8", errors="replace")
                while "\n" in buf:
                    line, buf = buf.split("\n", 1)
                    line = line.strip()
                    if not line.startswith("data:"):
                        continue
                    raw = line[5:].strip()
                    if not raw:
                        continue
                    try:
                        payload = json.loads(raw)
                    except json.JSONDecodeError:
                        continue
                    if "text" in payload:
                        full_text += payload["text"]
                        print(payload["text"], end="", flush=True)
                    if "error" in payload:
                        print(f"\n  {FAIL} SSE error: {payload['error']}")
                        raise RuntimeError(payload["error"])
    print()  # newline after streaming
    return full_text


def assert_ok(label: str, condition: bool, detail: str = "") -> None:
    if condition:
        print(f"  {PASS} {label}")
    else:
        print(f"  {FAIL} {label}" + (f": {detail}" if detail else ""))
        sys.exit(1)


# ─────────────────────────────────────────────────────────────
# Test steps
# ─────────────────────────────────────────────────────────────

def cleanup() -> None:
    """Remove any leftover data from previous test runs."""
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT CustomerId FROM Customer WHERE Email=?", (CUSTOMER_EMAIL,)
    ).fetchone()
    if row:
        cid = row["CustomerId"]
        # Delete in FK order
        conn.execute(
            "DELETE FROM InvoiceLine WHERE InvoiceId IN "
            "(SELECT InvoiceId FROM Invoice WHERE CustomerId=?)", (cid,)
        )
        conn.execute("DELETE FROM Invoice WHERE CustomerId=?", (cid,))
        conn.execute(
            "DELETE FROM messages WHERE conversation_id IN "
            "(SELECT conversation_id FROM conversations WHERE customer_id=?)", (cid,)
        )
        conn.execute("DELETE FROM conversations WHERE customer_id=?", (cid,))
        conn.execute("DELETE FROM purchase_requests WHERE customer_id=?", (cid,))
        conn.execute("DELETE FROM app_users WHERE email=?", (CUSTOMER_EMAIL,))
        conn.execute("DELETE FROM Customer WHERE CustomerId=?", (cid,))
    conn.commit()
    conn.close()
    print(f"  {INFO} DB cleaned")


def main() -> None:
    print("\n" + "=" * 60)
    print("  UC-1 Smoke Test — Iron Maiden Dual-Album Purchase")
    print("=" * 60)

    # ── 0. Health check ────────────────────────────────────────
    print("\n[0] Server health")
    r = httpx.get(f"{BASE}/health", timeout=5)
    assert_ok("Server is up", r.status_code == 200)

    # ── 1. Setup ───────────────────────────────────────────────
    print("\n[1] Setup — cleanup old test data")
    cleanup()

    # ── 2. Register customer ───────────────────────────────────
    print("\n[2] Register test customer")
    r = httpx.post(f"{BASE}/auth/register", json={
        "email": CUSTOMER_EMAIL, "password": CUSTOMER_PASSWORD,
        "first_name": "Alice", "last_name": "UC1Test",
        "city": "London", "country": "UK",
    })
    assert_ok("Registration returns 201", r.status_code == 201)
    customer_token = r.json()["data"]["token"]
    customer_id = r.json()["data"]["customer_id"]
    print(f"     CustomerId = {customer_id}")

    # ── 3. Admin login ─────────────────────────────────────────
    print("\n[3] Admin login")
    r = httpx.post(f"{BASE}/auth/login", json={
        "email": ADMIN_EMAIL, "password": ADMIN_PASSWORD,
    })
    assert_ok("Admin login 200", r.status_code == 200)
    assert_ok("Admin role", r.json()["data"]["role"] == "admin")
    admin_token = r.json()["data"]["token"]

    # ── 4. Browse — find the track ─────────────────────────────
    print("\n[4] Browse — find Hallowed Be Thy Name")
    resp = chat(customer_token,
                "Do you have 'Hallowed Be Thy Name' by Iron Maiden?")
    assert_ok("Agent mentions multiple versions",
              any(w in resp.lower() for w in ["version", "album", "live", "studio"]),
              resp[:120])

    # ── 5. Browse — compare two albums ────────────────────────
    print("\n[5] Browse — compare album track lists")
    resp = chat(customer_token,
                "Show me the track lists for The Number of The Beast "
                "and Rock In Rio CD2.")
    assert_ok("Response mentions track lists",
              "number of the beast" in resp.lower() or "rock in rio" in resp.lower(),
              resp[:120])

    # ── 6. Ask album prices ────────────────────────────────────
    print("\n[6] Ask album prices")
    resp = chat(customer_token,
                "How much for each full album?")
    assert_ok("Response contains a dollar amount",
              "$" in resp or "usd" in resp.lower() or "price" in resp.lower(),
              resp[:120])

    # ── 7. Buy both albums ─────────────────────────────────────
    print("\n[7] Customer requests purchase of both albums")
    resp = chat(customer_token, "I'll take both.")
    assert_ok("Agent confirms order under review",
              any(w in resp.lower() for w in [
                  "review", "pending", "submitted", "approval", "request"
              ]),
              resp[:200])

    # ── 8. Find the pending purchase request ──────────────────
    print("\n[8] Locate pending purchase request in DB")
    time.sleep(0.5)
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    pr = conn.execute(
        "SELECT purchase_request_id, total_usd, track_ids_json, status "
        "FROM purchase_requests WHERE customer_id=? AND status='pending'",
        (customer_id,),
    ).fetchone()
    conn.close()

    assert_ok("purchase_request row exists", pr is not None)
    assert_ok("Status is pending", pr["status"] == "pending")
    pr_id = pr["purchase_request_id"]
    total = pr["total_usd"]
    track_ids = json.loads(pr["track_ids_json"])
    print(f"     purchase_request_id={pr_id}, total=${total:.2f}, {len(track_ids)} tracks")
    assert_ok("Track count is plausible (both albums = 10–20 tracks)",
              10 <= len(track_ids) <= 25,
              f"got {len(track_ids)} tracks")

    # ── 9. Admin approves ──────────────────────────────────────
    print("\n[9] Admin approves purchase request")
    r = httpx.post(
        f"{BASE}/admin/purchases/{pr_id}/decision",
        headers={"Authorization": f"Bearer {admin_token}",
                 "Content-Type": "application/json"},
        json={"decision": "approved"},
    )
    assert_ok("Admin decision returns 200", r.status_code == 200, str(r.json()))
    assert_ok("Status flipped to approved",
              r.json()["data"]["status"] == "approved")

    # ── 10. Customer checks status ─────────────────────────────
    print("\n[10] Customer asks for order status (agent polls get_purchase_status)")
    resp = chat(customer_token, "What's the status of my order?")
    assert_ok("Agent detects approval",
              any(w in resp.lower() for w in [
                  "approved", "great news", "confirmed", "shipping",
                  "billing", "address", "ship"
              ]),
              resp[:200])

    # ── 11. Provide shipping details ──────────────────────────
    print("\n[11] Customer provides shipping address")
    resp = chat(customer_token,
                "Ship it to 42 Abbey Road, London, , UK, SW1A 1AA")
    assert_ok("Agent confirms purchase completion",
              any(w in resp.lower() for w in [
                  "invoice", "complete", "order", "confirmed", "enjoy",
                  "thank", "purchased", "success"
              ]),
              resp[:200])

    # ── 12. Verify Invoice in DB ───────────────────────────────
    print("\n[12] Verify Invoice and InvoiceLines in DB")
    time.sleep(0.5)
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row

    inv = conn.execute(
        "SELECT InvoiceId, Total FROM Invoice WHERE CustomerId=? "
        "ORDER BY InvoiceId DESC LIMIT 1",
        (customer_id,),
    ).fetchone()
    assert_ok("Invoice row created", inv is not None)

    lines = conn.execute(
        "SELECT COUNT(*) as n FROM InvoiceLine WHERE InvoiceId=?",
        (inv["InvoiceId"],),
    ).fetchone()
    conn.close()

    print(f"     InvoiceId={inv['InvoiceId']}, Total=${inv['Total']:.2f}, "
          f"{lines['n']} InvoiceLines")
    assert_ok("InvoiceLine count matches track count",
              lines["n"] == len(track_ids),
              f"expected {len(track_ids)}, got {lines['n']}")
    assert_ok("Invoice total matches purchase_request total",
              abs(inv["Total"] - total) < 0.01,
              f"Invoice=${inv['Total']:.2f} vs request=${total:.2f}")

    # ── 13. Verify purchase_request completed ─────────────────
    print("\n[13] Verify purchase_request marked completed")
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    pr_final = conn.execute(
        "SELECT status, invoice_id FROM purchase_requests WHERE purchase_request_id=?",
        (pr_id,),
    ).fetchone()
    conn.close()
    assert_ok("purchase_request status=completed", pr_final["status"] == "completed")
    assert_ok("purchase_request.invoice_id set", pr_final["invoice_id"] == inv["InvoiceId"])

    # ── Done ───────────────────────────────────────────────────
    print("\n" + "=" * 60)
    print(f"  {PASS} UC-1 PASSED — Invoice #{inv['InvoiceId']}, "
          f"{lines['n']} tracks, ${inv['Total']:.2f}")
    print("=" * 60 + "\n")


if __name__ == "__main__":
    main()
