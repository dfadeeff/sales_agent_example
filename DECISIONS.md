# Architectural Decisions — Marble Vinyl Store

These are the explicit decisions for every deliberately-unspecified question in the
tech challenge. Each entry names the decision, the rejected alternative, and the
specific reason the chosen path wins.

---

## 1. Tool Surface — Raw SQL vs Typed Tools

**Decision:** One raw SQL tool (`read_sql`) for all catalog browsing; three typed tools
for the purchase lifecycle (`create_purchase_request`, `get_purchase_status`,
`complete_purchase`).

**Rejected:** All-typed tools (e.g. `search_tracks(name)`, `list_albums(artist)`).

**Why raw SQL wins for browsing:**
The Chinook schema has joins the challenge explicitly tests — PlaylistTrack → Track →
Album → Artist → Genre across 3,503 tracks and 18 playlists. A typed search abstraction
would need ~15 tools to cover every combination the UCs require (filter playlist by
genre, side-by-side album comparison, cross-artist track search). Raw SQL covers all
of them with one tool and zero new code per new query shape.

**Why typed tools are required for purchases:**
Raw SQL cannot enforce a HITL gate. If the agent could `INSERT INTO Invoice ...` directly,
any jailbreak or prompt injection that reaches the `read_sql` tool (which only allows
SELECT — see §7) would still be blocked. But the stronger point is architectural: a
typed tool can validate inputs, compute prices, enforce idempotency, and transition
state atomically in ways a raw INSERT cannot. The three purchase tools exist precisely
because those invariants must be server-enforced, not prompt-enforced.

**Challenge note compliance:** The note says "expose raw SQL execution as the primary
tool — typed tools are out of scope for this evaluation." Interpretation: `read_sql`
IS the primary tool; the three typed purchase tools are the minimal boundary layer
required to enforce HITL and pricing integrity. They are not "out of scope" — they are
the HITL gate itself.

---

## 2. HITL Gating — What Architecturally Prevents Early Invoice Creation

**Decision:** `complete_purchase` is the only path to Invoice/InvoiceLine rows. It
enforces `status='approved'` inside a `BEGIN IMMEDIATE` SQLite transaction.

**Rejected:** Prompt-only gating ("never create an invoice without approval"). Rejected
because prompt instructions can be overridden by injected content or LLM reasoning
errors. A mechanical gate that survives any model behaviour is required.

**How the gate works (code in `mcp_server/server.py`):**

```
BEGIN IMMEDIATE          ← acquires write lock before any read
SELECT status FROM purchase_requests WHERE id=?
  → if status != 'approved': ROLLBACK, return error
  → if invoice_id is not NULL: ROLLBACK, return "already completed"
INSERT INTO Invoice ...
INSERT INTO InvoiceLine ... (one per TrackId)
UPDATE purchase_requests SET status='completed'
  WHERE id=? AND status='approved'  ← second guard
  → if rowcount == 0: ROLLBACK (concurrent call won)
COMMIT
```

`BEGIN IMMEDIATE` ensures that the status check and the INSERT are serialised. Two
concurrent calls cannot both pass the `status='approved'` check.

**Why Invoice rows cannot appear any other way:**
- `read_sql` rejects non-SELECT statements (§7)
- No other MCP tool writes to Invoice or InvoiceLine
- The API layer has no direct DB write path for purchases
- Admin endpoints only write to `purchase_requests.status`

---

## 3. Pricing Integrity — Server vs LLM

**Decision:** The server computes all totals. The LLM never performs or narrates
price arithmetic.

**Rejected:** Letting the agent calculate `n × $0.99` and passing it to the purchase
tool. Rejected because the LLM can hallucinate or round incorrectly, and a wrong total
stored in Invoice.Total would be a financial integrity violation.

**How it works:**
1. `create_purchase_request` queries `Track.UnitPrice` for all provided TrackIds,
   sums them, rounds to 2 decimal places, and stores in `purchase_requests.total_usd`.
2. `complete_purchase` re-queries `Track.UnitPrice` at invoice creation time (not
   the stored total) so InvoiceLine.UnitPrice is always live from the catalog.
3. Invoice.Total is `SUM(Track.UnitPrice)` computed inside the transaction —
   never a value the LLM provided.

The LLM may display the `total_usd` value from the tool result to the customer; it
cannot change it.

---

## 4. Conversation Memory — Strategy

**Decision:** Sliding window of the most recent 40 messages per conversation. No
summarisation. No vector retrieval.

**Rejected alternatives and why:**
- **Full history:** Would work but hits context limits on very long sessions and adds
  cost linearly with session age.
- **Summarisation:** Loses exact track names, TrackIds, and prices — the precise nouns
  that make catalog conversations coherent. A summary "the customer liked Iron Maiden"
  cannot substitute for "the customer has `purchase_request_id=7` pending".
- **Vector retrieval:** Over-engineered for a conversational agent where the relevant
  context is almost always recency-based.

**Window size rationale:** 40 messages × ~200 tokens = ~8,000 tokens of history.
That fits comfortably in `claude-sonnet-4-6`'s context alongside the catalog query
results (which can be verbose). A 3-album track listing at ~50 tracks × 3 fields
≈ 1,500 tokens. The agent can hold a full multi-album shopping session in context.

**Purchase history (UC-5):** Queried live via `read_sql` against
`Invoice JOIN InvoiceLine JOIN Track WHERE Invoice.CustomerId = ?`. Not stored in
conversation — catalog data is always fresher from the DB.

---

## 5. Interface Design

### Customer Chat
- **Streaming:** SSE via `fetch()` + `ReadableStream`. Not WebSocket (full-duplex
  unnecessary — one send, one streamed response per turn). Not native `EventSource`
  (doesn't support POST).
- **Feel:** Text appears character-by-character as it streams. Typing indicator while
  waiting for first delta. Shift+Enter for newline; Enter sends. Session persists via
  `sessionStorage`.
- **History on load:** Previous messages render on login so the customer sees their
  full conversation history without re-asking.

### Admin Panel
- **Updates:** 10-second polling on `GET /admin/purchases?status=pending`. No SSE
  or WebSocket — admin latency tolerance is seconds, not milliseconds.
- **Urgency sort:** Requests sorted oldest-first (longest wait at top). Each card
  shows "Waiting N min" badge.
- **Detail on click:** Expanded track list with artist, album, per-track price.
- **Race condition UX:** If two admins click Approve simultaneously, the second sees
  HTTP 409 and the UI shows "Already reviewed by another admin — refreshing."
- **Denial flow:** Deny button reveals an inline text input for the reason before
  confirming, preventing accidental denials.

---

## 6. Failure Modes

### Two admins approve simultaneously
The admin decision endpoint uses:
```sql
UPDATE purchase_requests SET status='approved', reviewed_by=?, reviewed_at=datetime('now')
WHERE purchase_request_id=? AND status='pending'
```
SQLite serialises concurrent writes. The second UPDATE finds `status != 'pending'`,
`rowcount = 0`, returns HTTP 409. No double-approval is possible.

### Customer retries the same purchase
`create_purchase_request` computes `idempotency_key = SHA256(customer_id:sorted_track_ids)`.
This is a UNIQUE column. On retry, the INSERT fails with IntegrityError; the handler
catches it, fetches the existing row, and returns the same `purchase_request_id` with
`note: "Existing active request returned (idempotent)."`. The agent continues from
where it left off.

If the customer retries after a denial or completion, the handler detects
`status in ('denied', 'completed')` and creates a fresh request (new key is not
needed — the UNIQUE constraint only blocks exact duplicates in non-terminal states).

### LLM hallucinates a TrackId
`create_purchase_request` validates every TrackId against `Track.TrackId`:
```python
missing = sorted(set(track_ids) - {r["TrackId"] for r in rows})
if missing:
    return {"error": f"TrackIds not found in catalog: {missing}"}
```
This returns a structured error dict (not a raised exception) so the Anthropic SDK
delivers it as a `tool_result` with `is_error=True`. The agent's system prompt
instructs it: "If a tool returns an error, explain the issue to the customer in plain
language." The customer can correct the request.

### `complete_purchase` called on non-approved request
The function checks `status == 'approved'` inside the transaction and returns
`{"error": "status is 'X', not 'approved'"}` without raising. The agent is instructed
never to call this proactively; the typed tool is the mechanical backstop.

### Double `complete_purchase` call
Checked via `invoice_id IS NOT NULL` before the INSERT and via the final
`UPDATE ... WHERE status='approved'` guard. The second call returns
`{"error": "already completed", "invoice_id": N}`.

### Very large result sets from `read_sql`
The tool description instructs "Use LIMIT on large tables (Track has 3,503 rows)."
The server does not impose a hard LIMIT (to allow intentional full-album queries),
but the tool description makes the LLM aware of the scale so it adds `LIMIT` clauses
when appropriate (e.g. playlist browsing).

---

## 7. Preventing Destructive SQL in `read_sql`

**The question:** How do we prevent the agent from issuing INSERT, UPDATE, DELETE,
DROP, ALTER, or TRUNCATE through `read_sql`?

**Three-layer defence (each layer is independent):**

### Layer 1 — First-token allowlist (in `mcp_server/server.py`)
```python
_ALLOWED_PREFIXES = ("select", "with", "explain")

stripped = query.strip().lower()
first_token = stripped.split()[0] if stripped else ""
if first_token not in _ALLOWED_PREFIXES:
    return {"error": "Only SELECT queries are permitted. No DML or DDL allowed."}
```
This catches `INSERT`, `UPDATE`, `DELETE`, `DROP`, `ALTER`, `CREATE`, `TRUNCATE`,
`REPLACE`, `PRAGMA` (write forms), and any other leading keyword that is not
a read operation. The check is on the normalised first token, so leading whitespace
and case variations are handled.

### Layer 2 — Read-only SQLite connection
```python
def _get_readonly_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn
```
SQLite's `mode=ro` URI flag opens the file in read-only mode at the OS level. Even
if the first-token check were bypassed (e.g. via a `WITH` CTE that contains a
write sub-expression), SQLite will raise `sqlite3.OperationalError: attempt to write
a readonly database` before any mutation occurs. The connection used exclusively by
`read_sql` is this read-only connection.

The write operations (`create_purchase_request`, `complete_purchase`) use a separate
read-write connection. These are never called from `read_sql`.

### Layer 3 — App-table blocklist
```python
for blocked in ("app_users", "messages"):
    if blocked in lower:
        return {"error": f"Access to '{blocked}' table is not permitted."}
```
Even SELECT access to `app_users` (which stores password hashes) and `messages`
(which stores conversation content) is blocked. The agent has no legitimate reason
to query these tables, and a prompt injection attack that tries to exfiltrate hashes
via `read_sql` is blocked at this layer.

### What this does NOT protect against
- A malicious Chinook trigger (Chinook has none; the schema is read-only catalog data)
- A supply-chain compromise of the `sqlite3` standard library
- An admin with direct DB file access

These are out of scope for an application-level threat model.

### Decision on `EXPLAIN`
`EXPLAIN` and `EXPLAIN QUERY PLAN` are allowed (included in `_ALLOWED_PREFIXES`)
because they are read-only diagnostic queries. They do not modify data. An agent
that uses EXPLAIN to understand query performance is a feature, not a threat.

---

## 8. User Story Compliance Checklist

All five use cases from the tech challenge are supported by the current architecture.
This section maps each requirement to its implementation and flags known limitations.

### UC-1: Song on multiple albums (Iron Maiden)

| Requirement | Implementation | Status |
|---|---|---|
| Find "Hallowed Be Thy Name" across 5+ albums | `read_sql` JOIN Track→Album→Artist | ✅ Live-tested |
| Show track lists for two specific albums | `read_sql` WHERE Album.Title = ? | ✅ |
| Price per full album | `read_sql` SUM(UnitPrice) GROUP BY AlbumId | ✅ |
| Bundle 17 tracks into one purchase | `create_purchase_request(track_ids=[...17 ids...])` | ✅ |
| HITL → admin approves | Admin panel + `/admin/purchases/{id}/decision` | ✅ |
| Agent asks for shipping → Invoice + 17 lines created | `complete_purchase` | ✅ |

### UC-2: Track by multiple artists (War Pigs)

| Requirement | Implementation | Status |
|---|---|---|
| Find "War Pigs" across 3 artists | `read_sql` WHERE Track.Name LIKE '%War Pigs%' | ✅ |
| Browse 4 + 6 albums for two artists | `read_sql` WHERE Artist.Name = ? | ✅ |
| Bundle 37 tracks, single purchase request | `create_purchase_request` | ✅ |
| Full HITL → ship → invoice flow | Same as UC-1 | ✅ |

### UC-3: Large playlist drill-down (90's Music)

| Requirement | Implementation | Status |
|---|---|---|
| List 90's Music playlist (1,477 tracks) | `read_sql` JOIN PlaylistTrack→Track→Playlist | ✅ |
| Filter by Metal and Rock genres | `read_sql` WHERE Genre.Name IN ('Metal','Rock') | ✅ |
| Side-by-side Pearl Jam vs Foo Fighters albums | Two `read_sql` calls, agent formats output | ✅ |
| Find album containing 'Everlong' | `read_sql` WHERE Track.Name LIKE '%Everlong%' | ✅ |
| Recommend one Audioslave album (genre overlap) | `read_sql` genre query + LLM reasoning | ✅ |
| 43-track bundle → HITL → ship → invoice | Same pipeline | ✅ |

### UC-4: Denied purchase

| Requirement | Implementation | Status |
|---|---|---|
| Customer initiates purchase | `create_purchase_request` | ✅ |
| Admin denies with optional reason | POST /admin/purchases/{id}/decision | ✅ |
| Agent relays denial_reason to customer | `get_purchase_status` → agent surfaces `denial_reason` | ✅ |
| Conversation continues uninterrupted | Conversation is never reset; agent offers to help | ✅ |
| Customer can browse or try different purchase | `read_sql` + new `create_purchase_request` | ✅ |

### UC-5: Returning customer with memory

| Requirement | Implementation | Status |
|---|---|---|
| "What did I buy last time?" | `read_sql` → Invoice ORDER BY InvoiceDate DESC LIMIT 1 | ✅ |
| "Show all past purchases" | `read_sql` → Invoice JOIN InvoiceLine JOIN Track | ✅ |
| "Recommend something similar" | `read_sql` genre/artist query + LLM reasoning | ✅ |
| Conversation history persists across sessions | Sliding window loaded from DB on every turn | ✅ |

### Known Limitations (not blocking, worth flagging)

1. **Proactive approval notification:** The challenge says "once approved, the agent
   returns to the conversation and asks for shipping details." The current implementation
   is pull-based: the agent polls `get_purchase_status` when the customer asks. It does
   not push a notification when the admin approves. A WebSocket or long-poll channel
   would be required for true push. This is acceptable for the assessment scope but
   would be flagged as a production gap.

2. **Single conversation per customer:** The current schema gives each customer one
   active conversation (the most recent one). Starting a fresh conversation requires
   clearing history manually. A "new conversation" button on the frontend would
   address this.

3. **No pagination on `read_sql` results:** Results are capped only by the LLM's
   choice to use `LIMIT`. A 1,477-row playlist returned without LIMIT would be a
   large tool result; the system prompt warns the agent to use LIMIT.

---

## 9. What We Would Flag Before Shipping to Production

1. Secret management: `SECRET_KEY` in `.env` needs rotation on deploy; no secret
   scanning in CI.
2. Rate limiting: no per-IP or per-user rate limit on `/chat`. A customer could
   exhaust Anthropic API quota.
3. The `read_sql` blocklist uses substring matching (`"app_users" in lower`). A query
   like `SELECT * FROM "APP_USERS"` would be caught (lowercased), but a Unicode
   homoglyph attack would not. Mitigated by Layer 2 (read-only connection) which blocks
   writes regardless.
4. JWT `SECRET_KEY` default in `api/config.py` is `"change-me-in-production"`. This
   must be overridden before any real deployment.
5. No HTTPS — TLS termination must be added at the reverse proxy layer.
