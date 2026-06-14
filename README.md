# Marble Vinyl Store — AI Sales Agent

A Human-in-the-Loop AI agent for a digital vinyl store. Customers browse the Chinook music catalog and request purchases via a streaming chat interface. Every purchase is held in a pending state until a sales-team admin approves or denies it. The invoice is only created after approval.

→ Full architectural decisions: [DECISIONS.md](DECISIONS.md)  
→ Project rules and house rules: [CLAUDE.md](CLAUDE.md)

---

## Architecture

```
Customer Chat (SSE streaming)       Admin Panel (10s polling)
        │                                   │
        ▼                                   ▼
        FastAPI — /auth  /chat  /checkout  /admin
                          │
                    Agent loop (Claude claude-sonnet-4-6)
                          │
                    MCP tools (4 tools)
                    ├── read_sql()                  ← raw SQL, SELECT only, read-only conn
                    ├── create_purchase_request()   ← validates TrackIds, computes total server-side
                    ├── get_purchase_status()       ← polls pending/approved/denied
                    └── complete_purchase()         ← HITL gate: enforces status='approved'
                          │
                    SQLite (Chinook + app tables)
```

**HITL gate:** `complete_purchase` uses `BEGIN IMMEDIATE` + `WHERE status='approved'` — the invoice cannot be created by any other path.  
**Pricing:** server reads `Track.UnitPrice` at request time and again at invoice creation. The LLM never computes or narrates a total.  
**Checkout:** after admin approval, a structured form appears in the customer's chat UI. `POST /checkout/{id}` creates the invoice directly — no address parsing by the LLM.

---

## Prerequisites

- Python 3.10+
- An Anthropic API key ([console.anthropic.com](https://console.anthropic.com))
- The Chinook SQLite database file

---

## Setup

### 1. Clone and enter the project

```bash
git clone https://github.com/dfadeeff/sales_agent_example.git
cd sales_agent_example
```

### 2. Download the Chinook database

```bash
curl -L -o Chinook_Sqlite.sqlite \
  "https://github.com/lerocha/chinook-database/raw/master/ChinookDatabase/DataSources/Chinook_Sqlite.sqlite"
```

### 3. Configure environment

```bash
cp .env.example .env
```

Edit `.env` and set your API key:

```
CLAUDE_API_KEY=sk-ant-api03-...
```

Optional: add SMTP settings to send real approval emails (otherwise they print to the server console).

### 4. Install dependencies

```bash
pip install -r requirements.txt
```

### 5. Initialise the database

Creates app tables and seeds admin accounts from the Chinook sales-team employees:

```bash
python setup.py
```

Output:
```
App tables created (or already exist).
  Seeded admin: Nancy Edwards <nancy@chinookcorp.com>
  Seeded admin: Jane Peacock <jane@chinookcorp.com>
  Seeded admin: Margaret Park <margaret@chinookcorp.com>
  Seeded admin: Steve Johnson <steve@chinookcorp.com>

Setup complete. Admin password: admin123
```

---

## Running

```bash
uvicorn api.main:app --reload --port 8000
```

Open two browser tabs:

| Tab | URL | Login |
|---|---|---|
| Customer chat | `http://localhost:8000` | Register a new account |
| Admin panel | `http://localhost:8000/admin.html` | `nancy@chinookcorp.com` / `admin123` |

---

## Walkthrough (UC-1)

1. **Customer tab** — register, then ask: *"Do you have Hallowed Be Thy Name by Iron Maiden?"*
2. Browse the 6 versions, ask for track lists and prices for two albums.
3. Say *"I'll take both"* — agent bundles 17 tracks and submits a purchase request.
4. **Admin tab** — the pending request appears (oldest first). Click **Approve**.
5. **Customer tab** — within 5 seconds a green toast fires and a checkout form appears in the chat.
6. Fill in a billing address and click **Complete Purchase — $16.83**.
7. Invoice #N is created. Verify: `sqlite3 Chinook_Sqlite.sqlite "SELECT * FROM Invoice ORDER BY InvoiceId DESC LIMIT 1;"`

---

## Testing

```bash
# UC-1: Iron Maiden dual-album (13 assertions)
python test_uc1.py

# UC-2 through UC-5 + UC-1 regression (requires server running)
python test_remaining_ucs.py
```

All five use cases from the challenge pass automated end-to-end:

| Test | Tracks | Total |
|---|---|---|
| UC-1 Iron Maiden dual-album | 17 | $16.83 |
| UC-2 War Pigs cross-artist | 2 | $1.98 |
| UC-3 90s playlist + recommendation | 43 | $42.57 |
| UC-4 Denied — conversation continues | — | — |
| UC-5 Returning customer + recommendations | — | — |

---

## Admin credentials

All four are seeded by `setup.py`. Default password for all: **`admin123`**

| Name | Email |
|---|---|
| Nancy Edwards (Sales Manager) | nancy@chinookcorp.com |
| Jane Peacock | jane@chinookcorp.com |
| Margaret Park | margaret@chinookcorp.com |
| Steve Johnson | steve@chinookcorp.com |

---

## Key files

| File | Purpose |
|---|---|
| `mcp_server/server.py` | 4 MCP tools — HITL gate, pricing, idempotency, SQL protection |
| `agent/loop.py` | Claude agentic loop — SSE streaming, sliding window memory, tool dispatch |
| `agent/tools.py` | Anthropic tool schemas + async dispatch (routes writes through shared DB connection) |
| `api/routers/checkout.py` | `POST /checkout/{id}` — structured checkout, verifies ownership + approved status |
| `api/routers/admin.py` | Admin decision endpoint — atomic `UPDATE WHERE status='pending'`, HTTP 409 on race |
| `api/routers/chat.py` | SSE chat + `GET /chat/notifications` (5s approval polling + email stub) |
| `frontend/chat.js` | Streaming fetch, checkout form, toast notifications |
| `frontend/admin.js` | Admin polling, approve/deny with inline denial reason |

---

## Deliberate design choices

See [DECISIONS.md](DECISIONS.md) for the full rationale on every unspecified question in the challenge: tool surface, HITL gating mechanism, pricing authority, conversation memory strategy, failure modes (double-approval race, retry idempotency, hallucinated TrackIds), and the three-layer SQL injection defence.
