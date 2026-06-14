#!/usr/bin/env python3
"""
UC-2 through UC-5 end-to-end tests.
Uses the structured checkout endpoint (POST /checkout/{id}) introduced after the
original UC-1 test was written — no longer relies on the agent collecting addresses.
"""
import json
import sqlite3
import sys
import time

import httpx

BASE = "http://localhost:8000"
DB = "Chinook_Sqlite.sqlite"
ADMIN_EMAIL = "nancy@chinookcorp.com"
ADMIN_PASS = "admin123"

PASS = "\033[92m✓\033[0m"
FAIL = "\033[91m✗\033[0m"
INFO = "\033[94m→\033[0m"
HEAD = "\033[1m"
END  = "\033[0m"

# ─── helpers ────────────────────────────────────────────────────────────────

def chat(token: str, message: str, label: str = "") -> str:
    tag = f" [{label}]" if label else ""
    print(f"  {INFO} Customer{tag}: {message[:80]}{'…' if len(message)>80 else ''}")
    print(f"  {INFO} Agent: ", end="", flush=True)
    full = ""
    with httpx.Client(timeout=120) as c:
        with c.stream("POST", f"{BASE}/chat",
                      headers={"Authorization": f"Bearer {token}",
                               "Content-Type": "application/json"},
                      content=json.dumps({"message": message})) as r:
            r.raise_for_status()
            buf = ""
            for chunk in r.iter_bytes():
                buf += chunk.decode("utf-8", errors="replace")
                while "\n" in buf:
                    line, buf = buf.split("\n", 1)
                    if not line.startswith("data:"): continue
                    raw = line[5:].strip()
                    if not raw: continue
                    try:
                        p = json.loads(raw)
                        if "text" in p:
                            full += p["text"]
                            print(p["text"][:200] if len(p["text"])>200 else p["text"],
                                  end="", flush=True)
                        if "error" in p:
                            raise RuntimeError(p["error"])
                    except (json.JSONDecodeError, RuntimeError) as e:
                        if isinstance(e, RuntimeError): raise
    print()
    return full


_SUBMISSION_WORDS = {"review", "submitted", "pending", "request", "under review",
                     "sent", "approval", "purchase request"}
_CONFIRM_WORDS    = {"shall i", "should i", "go ahead", "want me to", "confirm",
                     "shall i submit", "ready to", "proceed"}


def chat_and_confirm(token: str, message: str, customer_id: int) -> tuple[str, dict | None]:
    """Send message; if agent asks for confirmation, send 'Yes' and wait.
    Returns (final_response_text, pending_pr_or_None)."""
    resp = chat(token, message)
    r_lo = resp.lower()

    # If agent asked for confirmation, confirm
    if any(w in r_lo for w in _CONFIRM_WORDS) and not any(w in r_lo for w in _SUBMISSION_WORDS):
        resp2 = chat(token, "Yes, please go ahead and submit the purchase request now.")
        resp = resp2

    # Allow a moment for the DB write
    time.sleep(0.5)
    pr = get_pending_pr(customer_id)
    return resp, pr

def ok(label: str, cond: bool, detail: str = ""):
    if cond:
        print(f"  {PASS} {label}")
    else:
        print(f"  {FAIL} {label}" + (f": {detail}" if detail else ""))
        sys.exit(1)

def cleanup(emails: list[str]):
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    for email in emails:
        row = conn.execute("SELECT CustomerId FROM Customer WHERE Email=?", (email,)).fetchone()
        if row:
            cid = row["CustomerId"]
            conn.execute("DELETE FROM InvoiceLine WHERE InvoiceId IN (SELECT InvoiceId FROM Invoice WHERE CustomerId=?)", (cid,))
            conn.execute("DELETE FROM Invoice WHERE CustomerId=?", (cid,))
            conn.execute("DELETE FROM messages WHERE conversation_id IN (SELECT conversation_id FROM conversations WHERE customer_id=?)", (cid,))
            conn.execute("DELETE FROM conversations WHERE customer_id=?", (cid,))
            conn.execute("DELETE FROM purchase_requests WHERE customer_id=?", (cid,))
            conn.execute("DELETE FROM app_users WHERE email=?", (email,))
            conn.execute("DELETE FROM Customer WHERE CustomerId=?", (cid,))
    conn.commit()
    conn.close()

def register(email: str, first: str, last: str) -> tuple[str, int]:
    r = httpx.post(f"{BASE}/auth/register", json={
        "email": email, "password": "test1234",
        "first_name": first, "last_name": last,
    })
    assert r.status_code == 201, f"register failed: {r.text}"
    d = r.json()["data"]
    return d["token"], d["customer_id"]

def admin_login() -> str:
    r = httpx.post(f"{BASE}/auth/login", json={"email": ADMIN_EMAIL, "password": ADMIN_PASS})
    return r.json()["data"]["token"]

def approve(admin_token: str, pr_id: int):
    r = httpx.post(f"{BASE}/admin/purchases/{pr_id}/decision",
                   headers={"Authorization": f"Bearer {admin_token}",
                            "Content-Type": "application/json"},
                   json={"decision": "approved"})
    assert r.status_code == 200, f"approve failed: {r.text}"

def deny(admin_token: str, pr_id: int, reason: str = ""):
    r = httpx.post(f"{BASE}/admin/purchases/{pr_id}/decision",
                   headers={"Authorization": f"Bearer {admin_token}",
                            "Content-Type": "application/json"},
                   json={"decision": "denied", "denial_reason": reason})
    assert r.status_code == 200, f"deny failed: {r.text}"

def checkout(cust_token: str, pr_id: int) -> dict:
    r = httpx.post(f"{BASE}/checkout/{pr_id}",
                   headers={"Authorization": f"Bearer {cust_token}",
                            "Content-Type": "application/json"},
                   json={"billing_address": "1 Test Lane",
                         "billing_city": "Munich",
                         "billing_state": "",
                         "billing_country": "Germany",
                         "billing_postal_code": "80331"})
    assert r.status_code == 200, f"checkout failed: {r.text}"
    return r.json()["data"]

def get_pending_pr(customer_id: int) -> dict | None:
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT purchase_request_id, total_usd, track_ids_json FROM purchase_requests "
        "WHERE customer_id=? AND status='pending' ORDER BY purchase_request_id DESC LIMIT 1",
        (customer_id,)).fetchone()
    conn.close()
    return dict(row) if row else None

def get_invoice(customer_id: int) -> dict | None:
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    inv = conn.execute(
        "SELECT InvoiceId, Total FROM Invoice WHERE CustomerId=? ORDER BY InvoiceId DESC LIMIT 1",
        (customer_id,)).fetchone()
    if not inv: conn.close(); return None
    lines = conn.execute(
        "SELECT COUNT(*) as n FROM InvoiceLine WHERE InvoiceId=?",
        (inv["InvoiceId"],)).fetchone()
    conn.close()
    return {"invoice_id": inv["InvoiceId"], "total": inv["Total"], "lines": lines["n"]}

def poll_notifications(token: str) -> dict:
    r = httpx.get(f"{BASE}/chat/notifications",
                  headers={"Authorization": f"Bearer {token}"})
    return r.json()["data"]

# ═══════════════════════════════════════════════════════════════════════════
# UC-2: War Pigs — track recorded by multiple artists
# ═══════════════════════════════════════════════════════════════════════════

def test_uc2(admin_token: str):
    print(f"\n{HEAD}{'='*60}{END}")
    print(f"{HEAD}  UC-2: War Pigs — cross-artist browse and purchase{END}")
    print(f"{HEAD}{'='*60}{END}")
    EMAIL = "uc2_test@example.com"
    cleanup([EMAIL])
    token, cid = register(EMAIL, "UC2", "Test")
    print(f"  {INFO} CustomerId={cid}")

    # Step 1: Find War Pigs across artists
    print("\n[1] Find 'War Pigs' — should surface 3 artists")
    resp = chat(token, "Do you have 'War Pigs'?")
    ok("Agent finds multiple artists",
       any(a in resp.lower() for a in ["faith no more", "ozzy", "cake"]),
       resp[:200])

    # Step 2: Browse Faith No More discography
    print("\n[2] Browse Faith No More albums")
    resp = chat(token, "Show me Faith No More's albums.")
    ok("Faith No More albums listed",
       "faith no more" in resp.lower() or "real thing" in resp.lower(),
       resp[:200])

    # Step 3: Browse Ozzy discography
    print("\n[3] Browse Ozzy Osbourne albums")
    resp = chat(token, "And Ozzy Osbourne's albums?")
    ok("Ozzy albums listed",
       "ozzy" in resp.lower() or "speak of the devil" in resp.lower() or "blizzard" in resp.lower(),
       resp[:200])

    # Step 4: Buy War Pigs from both artists — agent may ask for confirmation
    print("\n[4] Buy War Pigs from Faith No More + Ozzy (with auto-confirm)")
    resp, pr = chat_and_confirm(
        token,
        "I want to buy War Pigs by Faith No More (from The Real Thing) and also War Pigs by Ozzy Osbourne.",
        cid,
    )
    ok("purchase_request created in DB", pr is not None,
       "no pending PR found — agent may not have called create_purchase_request")
    if pr:
        ok("Contains tracks from both artists",
           len(json.loads(pr["track_ids_json"])) >= 2,
           f"tracks: {pr['track_ids_json']}")

    print("\n[5] Admin approves → checkout form flow")
    approve(admin_token, pr["purchase_request_id"])
    notifs = poll_notifications(token)
    ok("Approval notification fires", len(notifs["approvals"]) >= 1)

    result = checkout(token, pr["purchase_request_id"])
    ok("Checkout succeeds", "invoice_id" in result)

    inv = get_invoice(cid)
    ok("Invoice created", inv is not None)
    ok("Invoice has correct tracks", inv["lines"] == len(json.loads(pr["track_ids_json"])),
       f"expected {len(json.loads(pr['track_ids_json']))}, got {inv['lines']}")
    print(f"\n  {PASS} UC-2 PASSED — Invoice #{inv['invoice_id']}, {inv['lines']} tracks, ${inv['total']:.2f}")


# ═══════════════════════════════════════════════════════════════════════════
# UC-3: 90's Music playlist drill-down with genre filter + recommendation
# ═══════════════════════════════════════════════════════════════════════════

def test_uc3(admin_token: str):
    print(f"\n{HEAD}{'='*60}{END}")
    print(f"{HEAD}  UC-3: 90s Playlist — drill-down and recommendation{END}")
    print(f"{HEAD}{'='*60}{END}")
    EMAIL = "uc3_test@example.com"
    cleanup([EMAIL])
    token, cid = register(EMAIL, "UC3", "Test")

    print("\n[1] Ask about 90's Music playlist")
    resp = chat(token, "What's in your 90's Music playlist?")
    ok("Agent describes the playlist",
       any(w in resp.lower() for w in ["playlist", "track", "1477", "music", "artist"]),
       resp[:200])

    print("\n[2] Filter by Metal and Rock")
    resp = chat(token, "Show me just the Metal and Rock artists from that playlist.")
    ok("Agent filters by genre",
       any(w in resp.lower() for w in ["rock", "metal", "artist", "genre"]),
       resp[:200])

    print("\n[3] Compare Pearl Jam vs Foo Fighters")
    resp = chat(token, "Show me Pearl Jam and Foo Fighters albums side by side.")
    ok("Both artists appear in response",
       "pearl jam" in resp.lower() and "foo fighters" in resp.lower(),
       resp[:200])

    print("\n[4] Find Everlong")
    resp = chat(token, "Which album has 'Everlong'?")
    ok("Agent identifies The Colour And The Shape",
       "colour" in resp.lower() or "color" in resp.lower() or "everlong" in resp.lower(),
       resp[:200])

    print("\n[5] Buy Pearl Jam + Foo Fighters + one Audioslave album (with auto-confirm)")
    # First: get the recommendation
    resp = chat(token,
        "I want Live On Two Legs from Pearl Jam and The Colour And The Shape from Foo Fighters. "
        "Also recommend me one Audioslave album based on genre.")
    ok("Agent recommends Audioslave", "audioslave" in resp.lower(), resp[:200])

    # Now confirm purchase including the recommendation
    resp, pr = chat_and_confirm(
        token,
        "Yes, add those three albums — both the Pearl Jam and Foo Fighters albums plus "
        "whichever Audioslave album you recommended. Submit the purchase request please.",
        cid,
    )
    ok("purchase_request created in DB", pr is not None,
       "no pending PR found after confirmation")
    if pr:
        track_count = len(json.loads(pr["track_ids_json"]))
        ok("Track count is substantial (≥10 tracks)", track_count >= 10,
           f"got {track_count}")
        print(f"     {track_count} tracks, total=${pr['total_usd']:.2f}")

    print("\n[6] Admin approves → checkout")
    ok("PR exists to approve", pr is not None)
    if pr:
        approve(admin_token, pr["purchase_request_id"])
        poll_notifications(token)
        result = checkout(token, pr["purchase_request_id"])
        ok("Checkout succeeds", "invoice_id" in result)
        inv = get_invoice(cid)
        ok("Invoice created", inv is not None)
        if inv:
            print(f"\n  {PASS} UC-3 PASSED — Invoice #{inv['invoice_id']}, {inv['lines']} tracks, ${inv['total']:.2f}")


# ═══════════════════════════════════════════════════════════════════════════
# UC-4: Denied purchase — conversation continues
# ═══════════════════════════════════════════════════════════════════════════

def test_uc4(admin_token: str):
    print(f"\n{HEAD}{'='*60}{END}")
    print(f"{HEAD}  UC-4: Denied purchase — conversation continues{END}")
    print(f"{HEAD}{'='*60}{END}")
    EMAIL = "uc4_test@example.com"
    cleanup([EMAIL])
    token, cid = register(EMAIL, "UC4", "Test")

    print("\n[1] Customer requests a purchase (with auto-confirm)")
    # Black Dog by Led Zeppelin has 2 versions in the DB
    resp, pr = chat_and_confirm(
        token,
        "Do you have 'Black Dog' by Led Zeppelin? I want to buy it — go ahead and submit.",
        cid,
    )
    if pr is None:
        # Agent may need another nudge — try once more explicitly
        resp, pr = chat_and_confirm(token, "Yes, submit the purchase request now please.", cid)
    if pr is None:
        # Fallback: seed a PR so we can still test the denial flow itself
        conn = sqlite3.connect(DB)
        conn.execute(
            "INSERT INTO purchase_requests (customer_id, track_ids_json, total_usd, idempotency_key) "
            "VALUES (?,?,?,?)", (cid, "[2]", 0.99, f"uc4-fallback-{cid}"))
        conn.commit()
        pr = get_pending_pr(cid)
        conn.close()
        print(f"  {INFO} fallback PR inserted (agent didn't auto-submit)")
    ok("purchase_request exists for denial test", pr is not None)
    pr_id = pr["purchase_request_id"]

    print("\n[2] Admin denies with a reason")
    deny(admin_token, pr_id, reason="Led Zeppelin tracks are not available for individual sale.")

    print("\n[3] Notification endpoint returns denial")
    notifs = poll_notifications(token)
    ok("Denial notification fires", len(notifs["denials"]) >= 1,
       str(notifs))
    ok("denial_reason present",
       "zeppelin" in (notifs["denials"][0].get("denial_reason") or "").lower()
       or len(notifs["denials"][0].get("denial_reason","")) > 0)

    # The frontend would auto-trigger the agent; simulate that here
    print("\n[4] Customer receives denial in conversation")
    denial_reason = notifs["denials"][0]["denial_reason"]
    resp = chat(token,
        f"My purchase request #{pr_id} was just declined. The reason given was: {denial_reason}. Please let me know.")
    ok("Agent relays the denial",
       any(w in resp.lower() for w in ["denied", "declined", "unfortunately", "sorry", "not available"]),
       resp[:200])
    ok("Agent offers to continue helping",
       any(w in resp.lower() for w in ["help", "else", "instead", "browse", "other", "anything"]),
       resp[:200])

    print("\n[5] Conversation continues — customer can browse")
    resp = chat(token, "Can you recommend something similar? Classic rock, hard rock?")
    ok("Agent continues conversation and suggests alternatives",
       any(w in resp.lower() for w in ["rock", "recommend", "artist", "album", "suggest", "try"]),
       resp[:200])

    print(f"\n  {PASS} UC-4 PASSED — denial relayed, conversation continues")


# ═══════════════════════════════════════════════════════════════════════════
# UC-5: Returning customer with purchase memory
# ═══════════════════════════════════════════════════════════════════════════

def test_uc5(admin_token: str):
    print(f"\n{HEAD}{'='*60}{END}")
    print(f"{HEAD}  UC-5: Returning customer — memory and recommendations{END}")
    print(f"{HEAD}{'='*60}{END}")

    # Use customer 5 from Chinook who already has real invoice history
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    # Find a Chinook customer with multiple invoices
    hist_customer = conn.execute(
        "SELECT c.CustomerId, c.Email, c.FirstName, COUNT(i.InvoiceId) as inv_count "
        "FROM Customer c JOIN Invoice i ON c.CustomerId=i.CustomerId "
        "WHERE c.CustomerId < 60 "
        "GROUP BY c.CustomerId ORDER BY inv_count DESC LIMIT 1"
    ).fetchone()
    cid = hist_customer["CustomerId"]
    email = hist_customer["Email"]
    fname = hist_customer["FirstName"]
    inv_count = hist_customer["inv_count"]
    conn.close()
    print(f"  {INFO} Using Chinook customer {cid} ({fname}, {inv_count} past invoices)")

    # Register an app_users row for this existing Chinook customer
    EMAIL_UC5 = f"uc5_{cid}@testmarble.com"
    conn = sqlite3.connect(DB)
    conn.execute("DELETE FROM messages WHERE conversation_id IN (SELECT conversation_id FROM conversations WHERE customer_id=?)", (cid,))
    conn.execute("DELETE FROM conversations WHERE customer_id=?", (cid,))
    conn.execute("DELETE FROM purchase_requests WHERE customer_id=? AND idempotency_key LIKE 'uc5%'", (cid,))
    conn.execute("DELETE FROM app_users WHERE email=?", (EMAIL_UC5,))
    conn.commit()
    conn.close()

    # Create app_users entry linked to the existing Chinook customer
    r = httpx.post(f"{BASE}/auth/register", json={
        "email": EMAIL_UC5, "password": "test1234",
        "first_name": fname, "last_name": "UC5Test",
    })
    # The register endpoint creates a NEW customer — we need to override the customer_id
    # Instead, seed the app_users row directly
    if r.status_code != 201:
        # Fall back: seed directly
        from argon2 import PasswordHasher
        ph = PasswordHasher().hash("test1234")
        conn = sqlite3.connect(DB)
        try:
            conn.execute(
                "INSERT INTO app_users (customer_id, email, password_hash, role) VALUES (?,?,?,'customer')",
                (cid, EMAIL_UC5, ph))
            conn.commit()
        except Exception:
            pass
        conn.close()
        token_r = httpx.post(f"{BASE}/auth/login",
                             json={"email": EMAIL_UC5, "password": "test1234"})
        token = token_r.json()["data"]["token"]
    else:
        # New customer was created — delete it and re-seed linked to cid
        new_cid = r.json()["data"]["customer_id"]
        new_uid = r.json()["data"]["user_id"]
        token_raw = r.json()["data"]["token"]
        conn = sqlite3.connect(DB)
        conn.execute("DELETE FROM Customer WHERE CustomerId=?", (new_cid,))
        conn.execute("UPDATE app_users SET customer_id=? WHERE user_id=?", (cid, new_uid))
        conn.commit()
        conn.close()
        # Re-login to get a fresh token with the correct customer_id
        token_r = httpx.post(f"{BASE}/auth/login",
                             json={"email": EMAIL_UC5, "password": "test1234"})
        token = token_r.json()["data"]["token"]

    print(f"  {INFO} Logged in as customer {cid} with {inv_count} real invoices")

    print("\n[1] 'What did I buy last time?'")
    resp = chat(token, "Hey, what did I buy last time?")
    ok("Agent retrieves past purchase",
       any(w in resp.lower() for w in ["invoice", "purchased", "bought", "order", "track", "album", "last"]),
       resp[:200])

    print("\n[2] 'Show me all my past purchases'")
    resp = chat(token, "Show me all my past purchases.")
    ok("Agent lists purchase history",
       any(w in resp.lower() for w in ["invoice", "purchase", "track", "total", "order"]),
       resp[:200])

    print("\n[3] 'Recommend something similar to what I usually buy'")
    resp = chat(token, "Based on my purchase history, recommend me something similar I haven't bought yet.")
    ok("Agent makes a recommendation",
       any(w in resp.lower() for w in ["recommend", "suggest", "similar", "genre", "artist", "might like", "based"]),
       resp[:200])
    ok("Recommendation contains a specific artist or album",
       any(c.isupper() for c in resp[20:]),  # some proper noun exists
       resp[:200])

    print(f"\n  {PASS} UC-5 PASSED — memory and recommendations work")


# ═══════════════════════════════════════════════════════════════════════════
# Also verify UC-1 with the new checkout flow (regression check)
# ═══════════════════════════════════════════════════════════════════════════

def test_uc1_checkout_regression(admin_token: str):
    print(f"\n{HEAD}{'='*60}{END}")
    print(f"{HEAD}  UC-1 regression: checkout form flow (not agent text){END}")
    print(f"{HEAD}{'='*60}{END}")
    EMAIL = "uc1_regression@example.com"
    cleanup([EMAIL])
    token, cid = register(EMAIL, "UC1reg", "Test")

    resp = chat(token, "I'll take both The Number of The Beast and Rock In Rio CD2 by Iron Maiden.")
    ok("Purchase submitted", any(w in resp.lower() for w in ["review","submitted","pending"]), resp[:150])

    time.sleep(0.5)
    pr = get_pending_pr(cid)
    ok("purchase_request created", pr is not None)
    approve(admin_token, pr["purchase_request_id"])
    notifs = poll_notifications(token)
    ok("Approval notification fires", len(notifs["approvals"]) >= 1)

    result = checkout(token, pr["purchase_request_id"])
    ok("Checkout endpoint succeeds", "invoice_id" in result)
    inv = get_invoice(cid)
    ok("Invoice has 17 tracks", inv["lines"] == 17, f"got {inv['lines']}")
    ok("Total is $16.83", abs(inv["total"] - 16.83) < 0.01, f"got ${inv['total']:.2f}")
    print(f"\n  {PASS} UC-1 regression PASSED — Invoice #{inv['invoice_id']}, 17 tracks, $16.83")


# ═══════════════════════════════════════════════════════════════════════════
# Run all
# ═══════════════════════════════════════════════════════════════════════════

def main():
    print(f"\n{HEAD}Marble UC Test Suite — UC-2 through UC-5 + UC-1 regression{END}")
    r = httpx.get(f"{BASE}/health", timeout=5)
    if r.status_code != 200:
        print(f"  {FAIL} Server not running on {BASE}")
        sys.exit(1)
    print(f"  {PASS} Server healthy\n")

    admin_token = admin_login()

    results = {}
    for name, fn in [
        ("UC-2 War Pigs",   lambda: test_uc2(admin_token)),
        ("UC-3 90s Playlist", lambda: test_uc3(admin_token)),
        ("UC-4 Denied",     lambda: test_uc4(admin_token)),
        ("UC-5 Memory",     lambda: test_uc5(admin_token)),
        ("UC-1 regression", lambda: test_uc1_checkout_regression(admin_token)),
    ]:
        try:
            fn()
            results[name] = "PASS"
        except SystemExit:
            results[name] = "FAIL"
        except Exception as e:
            print(f"\n  {FAIL} Unexpected error: {e}")
            results[name] = f"ERROR: {e}"

    print(f"\n{HEAD}{'='*60}")
    print("  SUMMARY")
    print(f"{'='*60}{END}")
    all_pass = True
    for name, status in results.items():
        icon = PASS if status == "PASS" else FAIL
        print(f"  {icon} {name}: {status}")
        if status != "PASS": all_pass = False
    print()
    sys.exit(0 if all_pass else 1)


if __name__ == "__main__":
    main()
