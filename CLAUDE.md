# CLAUDE.md — Marble Vinyl Store

> **Full architectural decisions and rationale:** [DECISIONS.md](DECISIONS.md)

## 1. Self Verification

Before marking any task done, run all four:

```bash
# 1. Install deps and init DB
pip install -r requirements.txt
python setup.py

# 2. Unit tests
python -m pytest tests/ -x -q

# 3. Type check
python -m mypy api/ mcp_server/ agent/ --ignore-missing-imports

# 4. Manual smoke test (UC-1)
#    a. Start server: uvicorn api.main:app --reload
#    b. Open http://localhost:8000/customer.html
#    c. Register a new account, log in
#    d. Chat: "Do you have Hallowed Be Thy Name by Iron Maiden?"
#    e. Buy two albums (Number of the Beast + Rock in Rio CD2)
#    f. Open http://localhost:8000/admin.html, log in as nancy@chinookcorp.com / admin123
#    g. Approve the purchase request
#    h. Back in customer chat: "What's the status of my order?" → provide shipping details
#    i. Confirm in DB: sqlite3 Chinook_Sqlite.sqlite "SELECT * FROM Invoice ORDER BY InvoiceId DESC LIMIT 1;"
```

---

## 2. Architecture Plan

```
Customer Chat UI          Admin Approval Panel
 (frontend/customer.html)  (frontend/admin.html)
        │  fetch + SSE              │  10s polling
        ▼                           ▼
   POST /chat              GET+POST /admin/purchases
        │                           │
        ▼─────────────────────────▼
              FastAPI (api/main.py)
              ├── /auth  (routers/auth.py)
              ├── /chat  (routers/chat.py)
              └── /admin (routers/admin.py)
                          │
              AgentLoop (agent/loop.py)
              ├── Sliding window: last 40 messages
              ├── System prompt injected fresh each turn
              └── Tool dispatch → mcp_server/server.py
                          │
              MCP Tools (mcp_server/server.py)
              ├── read_sql()              SELECT only
              ├── create_purchase_request()  validates TrackIds, computes total
              ├── get_purchase_status()      polls purchase_requests table
              └── complete_purchase()        HITL gate + Invoice creation
                          │
              Chinook_Sqlite.sqlite (single file)
              ├── Chinook tables (read for catalog, append-only for Invoice)
              └── App tables: app_users, conversations, messages, purchase_requests
```

**Key decisions:**
- `read_sql` is raw SQL (expressive catalog browsing). Purchase tools are typed (HITL gate cannot be raw SQL).
- `complete_purchase` uses `BEGIN IMMEDIATE` + `WHERE status='approved'` — gate is DB-level, not prompt-level.
- Pricing computed by server in `create_purchase_request`; stored in `purchase_requests.total_usd`; never recomputed by LLM.
- Memory: sliding window of 40 messages. No summarisation — catalog queries need exact track names.
- Admin users seeded from Chinook Employee table (sales team). Default password: `admin123`.

---

## 3. Permissions Gate

**The LLM (agent) MAY:**
- `read_sql(query)` — SELECT or WITH only; any other statement is rejected
- `create_purchase_request(customer_id, track_ids)` — creates pending request
- `get_purchase_status(purchase_request_id)` — read-only status check
- `complete_purchase(purchase_request_id, billing_*)` — only succeeds when `status='approved'`

**The LLM MAY NOT:**
- Execute INSERT / UPDATE / DELETE / DDL directly — blocked by **three independent layers**:
  1. First-token allowlist in `read_sql` rejects anything not starting with `SELECT`/`WITH`/`EXPLAIN`
  2. `read_sql` opens a `mode=ro` SQLite URI connection — OS-level read-only; writes raise `OperationalError`
  3. `app_users` and `messages` tables are substring-blocked even for SELECT
- Create `Invoice` or `InvoiceLine` rows except through `complete_purchase`
- Approve or deny its own purchase requests (no tool exists for that)
- Know the customer's password hash or JWT secret

→ Full analysis: [DECISIONS.md §7](DECISIONS.md#7-preventing-destructive-sql-in-read_sql)

**Human destructive command gate (for developers):**
Never run any of the following without a confirmed DB backup and explicit user instruction:
- `DROP TABLE` on any Chinook table
- `DELETE FROM Customer`, `Invoice`, `InvoiceLine`, `Track`, `Album`, `Artist`
- `ALTER TABLE` on existing Chinook tables
- `rm Chinook_Sqlite.sqlite` or any truncation of the DB file

---

## 4. House Rules

1. **Pricing authority**: prices always come from `Track.UnitPrice` in Chinook. `purchase_requests.total_usd` is set once at creation and never changed. The LLM displays totals from tool results only.

2. **Tool errors as dicts**: MCP tools return `{"error": "..."}` dicts on failure — never raise exceptions to the Anthropic SDK. The agent must relay errors to the customer in plain language.

3. **One-way status transitions**: `pending → approved/denied → completed`. No backwards transitions. Ever.

4. **System prompt is ephemeral**: never stored in the `messages` table. Injected fresh every turn with the current `customer_id`.

5. **Agent separate from HTTP layer**: `agent/loop.py` contains all agentic logic. `api/routers/chat.py` only marshals the HTTP request and streams the response. No business logic in routers.

6. **Structured API responses**: success → `{"data": ..., "error": null}`. failure → `{"data": null, "error": "..."}`. HTTP status codes used correctly (400 client error, 401 unauthorized, 403 forbidden, 404 not found, 409 conflict, 503 service unavailable).

7. **Admin JWT gate**: all `/admin/*` endpoints call `require_admin` dependency in `api/auth.py`. A customer token must never reach admin handlers.

8. **Adding a new MCP tool**: update `mcp_server/server.py` first, then `agent/tools.py` (add schema entry), then update the Permissions Gate section above. All three in the same commit.
