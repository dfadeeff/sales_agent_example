from typing import Annotated

import aiosqlite
from fastapi import APIRouter, Depends, HTTPException, Path
from pydantic import BaseModel

from api.auth import get_current_user
from api.db import get_db
from mcp_server.server import async_complete_purchase

router = APIRouter(prefix="/checkout", tags=["checkout"])


class CheckoutRequest(BaseModel):
    billing_address: str
    billing_city: str
    billing_state: str = ""
    billing_country: str
    billing_postal_code: str = ""


@router.post("/{purchase_request_id}")
async def checkout(
    purchase_request_id: Annotated[int, Path()],
    body: CheckoutRequest,
    user: Annotated[dict, Depends(get_current_user)],
    db: Annotated[aiosqlite.Connection, Depends(get_db)],
) -> dict:
    if user["role"] != "customer":
        raise HTTPException(status_code=403, detail="Customers only")

    # Verify this request belongs to the logged-in customer
    async with db.execute(
        "SELECT customer_id, status FROM purchase_requests WHERE purchase_request_id = ?",
        (purchase_request_id,),
    ) as cur:
        row = await cur.fetchone()

    if not row:
        raise HTTPException(status_code=404, detail="Purchase request not found")
    if row["customer_id"] != user["customer_id"]:
        raise HTTPException(status_code=403, detail="Not your purchase request")
    if row["status"] != "approved":
        raise HTTPException(
            status_code=400,
            detail=f"Cannot checkout: status is '{row['status']}', not 'approved'",
        )

    result = await async_complete_purchase(
        db,
        purchase_request_id,
        body.billing_address,
        body.billing_city,
        body.billing_state,
        body.billing_country,
        body.billing_postal_code,
    )

    if "error" in result:
        raise HTTPException(status_code=400, detail=result["error"])

    # Persist a completion message to the conversation so chat history is accurate
    async with db.execute(
        "SELECT conversation_id FROM conversations WHERE customer_id=? ORDER BY updated_at DESC LIMIT 1",
        (user["customer_id"],),
    ) as cur:
        conv_row = await cur.fetchone()

    if conv_row:
        import json
        msg = (
            f"Your purchase is complete! Invoice #{result['invoice_id']} has been created "
            f"for {result['track_count']} track(s) totalling ${result['total_usd']:.2f}. "
            f"Enjoy the music!"
        )
        await db.execute(
            "INSERT INTO messages (conversation_id, role, content) VALUES (?, 'assistant', ?)",
            (conv_row["conversation_id"], json.dumps(msg)),
        )
        await db.execute(
            "UPDATE conversations SET updated_at=datetime('now') WHERE conversation_id=?",
            (conv_row["conversation_id"],),
        )

    return {"data": result, "error": None}
