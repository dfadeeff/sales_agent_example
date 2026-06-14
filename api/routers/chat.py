import json
from typing import Annotated, AsyncIterator

import aiosqlite
from fastapi import APIRouter, Depends
from fastapi.responses import StreamingResponse

from agent.loop import run_agent_turn
from api.auth import get_current_user
from api.db import get_db
from api.models import ChatRequest

router = APIRouter(prefix="/chat", tags=["chat"])


async def _send_approval_email(customer_email: str, customer_name: str, total_usd: float) -> None:
    """Send approval notification email. Falls back to console log for local dev."""
    import os
    subject = "Your Marble order has been approved!"
    body = (
        f"Hi {customer_name},\n\n"
        f"Great news — your order totalling ${total_usd:.2f} has been approved.\n"
        f"Open your chat at http://localhost:8000 to provide your shipping details "
        f"and complete the purchase.\n\nThe Marble Team"
    )
    smtp_host = os.environ.get("SMTP_HOST", "")
    if smtp_host:
        import smtplib
        from email.mime.text import MIMEText
        msg = MIMEText(body)
        msg["Subject"] = subject
        msg["From"] = os.environ.get("SMTP_FROM", "noreply@marble.store")
        msg["To"] = customer_email
        try:
            with smtplib.SMTP(smtp_host, int(os.environ.get("SMTP_PORT", "587"))) as s:
                s.starttls()
                s.login(os.environ.get("SMTP_USER", ""), os.environ.get("SMTP_PASS", ""))
                s.sendmail(msg["From"], [customer_email], msg.as_string())
            print(f"[EMAIL] Sent approval notification to {customer_email}")
        except Exception as e:
            print(f"[EMAIL FAILED] {e} — would have sent to {customer_email}")
    else:
        # Local dev: print to console
        print(f"\n{'='*60}")
        print(f"[EMAIL NOTIFICATION] To: {customer_email}")
        print(f"Subject: {subject}")
        print(body)
        print(f"{'='*60}\n")


async def _sse_stream(
    db: aiosqlite.Connection,
    customer_id: int,
    message: str,
) -> AsyncIterator[str]:
    try:
        async for chunk in run_agent_turn(db, customer_id, message):
            yield chunk
    except Exception as e:
        yield f"data: {json.dumps({'error': str(e)})}\n\n"


@router.post("")
async def chat(
    body: ChatRequest,
    user: Annotated[dict, Depends(get_current_user)],
    db: Annotated[aiosqlite.Connection, Depends(get_db)],
) -> StreamingResponse:
    if user["role"] != "customer":
        from fastapi import HTTPException
        raise HTTPException(status_code=403, detail="Chat is for customers only")

    return StreamingResponse(
        _sse_stream(db, user["customer_id"], body.message),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


@router.get("/history")
async def get_history(
    user: Annotated[dict, Depends(get_current_user)],
    db: Annotated[aiosqlite.Connection, Depends(get_db)],
) -> dict:
    if user["role"] != "customer":
        from fastapi import HTTPException
        raise HTTPException(status_code=403, detail="Chat history is for customers only")

    # Get or create conversation
    async with db.execute(
        "SELECT conversation_id FROM conversations WHERE customer_id=? ORDER BY updated_at DESC LIMIT 1",
        (user["customer_id"],),
    ) as cur:
        row = await cur.fetchone()

    if not row:
        return {"data": {"conversation_id": None, "messages": []}, "error": None}

    conversation_id = row["conversation_id"]

    async with db.execute(
        """SELECT role, content, created_at FROM messages
           WHERE conversation_id = ?
           ORDER BY created_at ASC""",
        (conversation_id,),
    ) as cur:
        rows = await cur.fetchall()

    messages = []
    for r in rows:
        content = json.loads(r["content"])
        # Flatten to plain text for display
        if isinstance(content, list):
            text = " ".join(
                block.get("text", "") for block in content if block.get("type") == "text"
            )
        else:
            text = str(content)
        messages.append({
            "role": r["role"],
            "content": text,
            "created_at": r["created_at"],
        })

    return {
        "data": {"conversation_id": conversation_id, "messages": messages},
        "error": None,
    }


@router.get("/notifications")
async def get_notifications(
    user: Annotated[dict, Depends(get_current_user)],
    db: Annotated[aiosqlite.Connection, Depends(get_db)],
) -> dict:
    """
    Poll for purchase requests that have just been approved but not yet notified.
    Returns them and marks them as notified atomically.
    The frontend polls this every 5 s and auto-triggers the agent when an approval arrives.
    """
    if user["role"] != "customer":
        return {"data": {"approvals": [], "denials": []}, "error": None}

    customer_id = user["customer_id"]

    # Fetch newly approved requests (not yet notified)
    async with db.execute(
        """SELECT pr.purchase_request_id, pr.total_usd,
                  c.FirstName || ' ' || c.LastName AS customer_name,
                  c.Email AS customer_email
           FROM purchase_requests pr
           JOIN Customer c ON pr.customer_id = c.CustomerId
           WHERE pr.customer_id = ? AND pr.status = 'approved'
             AND (pr.notification_sent IS NULL OR pr.notification_sent = 0)""",
        (customer_id,),
    ) as cur:
        approved_rows = await cur.fetchall()

    # Fetch newly denied requests (not yet notified)
    async with db.execute(
        """SELECT purchase_request_id, denial_reason
           FROM purchase_requests
           WHERE customer_id = ? AND status = 'denied'
             AND (notification_sent IS NULL OR notification_sent = 0)""",
        (customer_id,),
    ) as cur:
        denied_rows = await cur.fetchall()

    approvals = []
    for row in approved_rows:
        approvals.append({
            "purchase_request_id": row["purchase_request_id"],
            "total_usd": row["total_usd"],
        })
        # Mark notified
        await db.execute(
            "UPDATE purchase_requests SET notification_sent=1 WHERE purchase_request_id=?",
            (row["purchase_request_id"],),
        )
        # Fire email (non-blocking — errors are logged, not raised)
        try:
            await _send_approval_email(row["customer_email"], row["customer_name"], row["total_usd"])
        except Exception as e:
            print(f"[NOTIFY] Email error: {e}")

    denials = []
    for row in denied_rows:
        denials.append({
            "purchase_request_id": row["purchase_request_id"],
            "denial_reason": row["denial_reason"] or "",
        })
        await db.execute(
            "UPDATE purchase_requests SET notification_sent=1 WHERE purchase_request_id=?",
            (row["purchase_request_id"],),
        )

    return {"data": {"approvals": approvals, "denials": denials}, "error": None}
