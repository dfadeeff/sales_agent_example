"""
run_agent_turn() — the core agentic loop.
Receives one user message, runs the Claude tool-calling loop, and yields SSE chunks.
"""
import json
from typing import AsyncIterator

import aiosqlite
import anthropic

from agent.tools import TOOL_SCHEMAS, dispatch_tool
from api.config import settings

MODEL = "claude-sonnet-4-6"
MAX_HISTORY = 40

SYSTEM_PROMPT_TEMPLATE = """\
You are the Marble vinyl store assistant. You help customers discover and purchase music.

The customer's CustomerId is {customer_id}. Always pass this exact value to create_purchase_request.

Guidelines:
- Browse the catalog freely using read_sql (SELECT queries only).
- When a customer wants to buy, call create_purchase_request with their CustomerId and the TrackIds.
  Never construct INSERT or UPDATE statements.
- After submitting a purchase request, tell the customer it is under review.
  Do not call get_purchase_status at all — the UI handles approval notifications automatically.
- When you receive a system message that the order was approved, tell the customer their
  checkout form has appeared in the chat — they should fill it in to complete the purchase.
  Do NOT ask for an address — the form collects it directly.
- When status is 'denied': relay the denial_reason if provided, then offer to help further.
- Never expose SQL queries, internal IDs, or tool names in your responses.
- Never compute or estimate prices — always use the totals from tool results.
- If a tool returns an error, explain the issue to the customer in plain language.
- Keep responses conversational and helpful. The customer is browsing a music store."""


async def _load_history(
    db: aiosqlite.Connection, conversation_id: int
) -> list[dict]:
    """Load last MAX_HISTORY messages, return as Anthropic message format."""
    async with db.execute(
        """SELECT role, content FROM messages
           WHERE conversation_id = ?
           ORDER BY created_at DESC
           LIMIT ?""",
        (conversation_id, MAX_HISTORY),
    ) as cur:
        rows = await cur.fetchall()

    messages = []
    for row in reversed(rows):
        content = json.loads(row["content"])
        messages.append({"role": row["role"], "content": content})
    return messages


async def _save_message(
    db: aiosqlite.Connection, conversation_id: int, role: str, content
) -> int:
    async with db.execute(
        "INSERT INTO messages (conversation_id, role, content) VALUES (?, ?, ?)",
        (conversation_id, role, json.dumps(content)),
    ) as cur:
        msg_id = cur.lastrowid
    await db.execute(
        "UPDATE conversations SET updated_at=datetime('now') WHERE conversation_id=?",
        (conversation_id,),
    )
    await db.commit()
    return msg_id


async def _get_or_create_conversation(
    db: aiosqlite.Connection, customer_id: int
) -> int:
    async with db.execute(
        "SELECT conversation_id FROM conversations WHERE customer_id=? ORDER BY updated_at DESC LIMIT 1",
        (customer_id,),
    ) as cur:
        row = await cur.fetchone()
    if row:
        return row["conversation_id"]
    async with db.execute(
        "INSERT INTO conversations (customer_id) VALUES (?)", (customer_id,)
    ) as cur:
        conv_id = cur.lastrowid
    await db.commit()
    return conv_id


async def run_agent_turn(
    db: aiosqlite.Connection,
    customer_id: int,
    user_message: str,
) -> AsyncIterator[str]:
    """
    Process one customer message through the agentic loop.
    Yields SSE-formatted strings: 'data: {...}\n\n'
    """
    client = anthropic.Anthropic(api_key=settings.effective_api_key)

    conversation_id = await _get_or_create_conversation(db, customer_id)
    await _save_message(db, conversation_id, "user", user_message)

    messages = await _load_history(db, conversation_id)
    system = SYSTEM_PROMPT_TEMPLATE.format(customer_id=customer_id)

    last_message_id = None
    loop_count = 0

    while loop_count < 10:  # safety cap on tool-call loops
        loop_count += 1
        full_response_blocks = []
        text_buffer = ""
        stop_reason = None

        with client.messages.stream(
            model=MODEL,
            max_tokens=4096,
            system=system,
            messages=messages,
            tools=TOOL_SCHEMAS,
        ) as stream:
            for event in stream:
                if hasattr(event, "type"):
                    if event.type == "content_block_delta":
                        delta = event.delta
                        if hasattr(delta, "text"):
                            text_buffer += delta.text
                            yield f"data: {json.dumps({'text': delta.text})}\n\n"
                    elif event.type == "message_stop":
                        pass

            final_message = stream.get_final_message()
            stop_reason = final_message.stop_reason
            # Serialize only the fields the API accepts back (no internal SDK fields)
            full_response_blocks = []
            for b in final_message.content:
                if b.type == "text":
                    full_response_blocks.append({"type": "text", "text": b.text})
                elif b.type == "tool_use":
                    full_response_blocks.append({
                        "type": "tool_use",
                        "id": b.id,
                        "name": b.name,
                        "input": b.input,
                    })

        # Persist the full assistant turn (may include tool_use blocks)
        last_message_id = await _save_message(
            db, conversation_id, "assistant", full_response_blocks
        )

        if stop_reason != "tool_use":
            break

        # Execute tool calls and collect results
        tool_results = []
        for block in full_response_blocks:
            if block.get("type") == "tool_use":
                tool_name = block["name"]
                tool_input = block["input"]
                tool_use_id = block["id"]

                # Run the tool — write tools receive the shared db connection
                result_str = await dispatch_tool(tool_name, tool_input, db=db)

                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": tool_use_id,
                    "content": result_str,
                })

        # Persist tool_results so the next turn reloads a valid message sequence:
        # assistant:[tool_use] must be immediately followed by user:[tool_result].
        # Without this, loading history on the next turn produces orphan tool_use
        # blocks that the API rejects with "tool_use ids found without tool_result".
        await _save_message(db, conversation_id, "user", tool_results)

        # Append both to the in-memory list for the next iteration of this loop
        messages.append({"role": "assistant", "content": full_response_blocks})
        messages.append({"role": "user", "content": tool_results})

    yield f"data: {json.dumps({'done': True, 'message_id': last_message_id})}\n\n"
