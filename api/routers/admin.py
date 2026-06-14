import json
from typing import Annotated

import aiosqlite
from fastapi import APIRouter, Depends, HTTPException, Path, Query

from api.auth import require_admin
from api.db import get_db
from api.models import AdminDecisionRequest

router = APIRouter(prefix="/admin", tags=["admin"])


@router.get("/purchases")
async def list_purchases(
    user: Annotated[dict, Depends(require_admin)],
    db: Annotated[aiosqlite.Connection, Depends(get_db)],
    status: str = Query(default="pending"),
) -> dict:
    valid_statuses = ("pending", "approved", "denied", "completed")
    if status not in valid_statuses:
        raise HTTPException(status_code=400, detail=f"status must be one of {valid_statuses}")

    async with db.execute(
        """SELECT
               pr.purchase_request_id,
               pr.status,
               pr.total_usd,
               pr.track_ids_json,
               pr.denial_reason,
               pr.created_at,
               pr.invoice_id,
               c.FirstName || ' ' || c.LastName AS customer_name,
               c.Email AS customer_email,
               CAST((julianday('now') - julianday(pr.created_at)) * 1440 AS INTEGER) AS wait_minutes
           FROM purchase_requests pr
           JOIN Customer c ON pr.customer_id = c.CustomerId
           WHERE pr.status = ?
           ORDER BY pr.created_at ASC""",
        (status,),
    ) as cur:
        rows = await cur.fetchall()

    items = []
    for row in rows:
        track_ids = json.loads(row["track_ids_json"])

        # Fetch track details for display
        placeholders = ",".join("?" * len(track_ids))
        async with db.execute(
            f"""SELECT t.TrackId, t.Name as track_name, t.UnitPrice,
                       ar.Name as artist_name, al.Title as album_title
                FROM Track t
                JOIN Album al ON t.AlbumId = al.AlbumId
                JOIN Artist ar ON al.ArtistId = ar.ArtistId
                WHERE t.TrackId IN ({placeholders})""",
            track_ids,
        ) as cur2:
            track_rows = await cur2.fetchall()

        items.append({
            "purchase_request_id": row["purchase_request_id"],
            "status": row["status"],
            "total_usd": row["total_usd"],
            "track_count": len(track_ids),
            "customer_name": row["customer_name"],
            "customer_email": row["customer_email"],
            "created_at": row["created_at"],
            "wait_minutes": row["wait_minutes"],
            "denial_reason": row["denial_reason"],
            "invoice_id": row["invoice_id"],
            "line_items": [dict(r) for r in track_rows],
        })

    return {"data": {"items": items}, "error": None}


@router.post("/purchases/{purchase_id}/decision")
async def decide_purchase(
    purchase_id: Annotated[int, Path()],
    body: AdminDecisionRequest,
    user: Annotated[dict, Depends(require_admin)],
    db: Annotated[aiosqlite.Connection, Depends(get_db)],
) -> dict:
    # First verify the request exists
    async with db.execute(
        "SELECT status FROM purchase_requests WHERE purchase_request_id = ?",
        (purchase_id,),
    ) as cur:
        existing = await cur.fetchone()

    if not existing:
        raise HTTPException(status_code=404, detail="Purchase request not found")

    if existing["status"] != "pending":
        raise HTTPException(
            status_code=409,
            detail="Already reviewed by another admin",
        )

    # Atomic status transition — WHERE status='pending' is the final guard
    async with db.execute(
        """UPDATE purchase_requests
           SET status = ?,
               denial_reason = ?,
               reviewed_by = ?,
               reviewed_at = datetime('now'),
               updated_at = datetime('now')
           WHERE purchase_request_id = ? AND status = 'pending'""",
        (
            body.decision,
            body.denial_reason if body.decision == "denied" else None,
            user["user_id"],
            purchase_id,
        ),
    ) as cur:
        if cur.rowcount == 0:
            # Another admin raced us between the SELECT and UPDATE
            raise HTTPException(
                status_code=409,
                detail="Already reviewed by another admin",
            )

    await db.commit()

    return {
        "data": {
            "purchase_request_id": purchase_id,
            "status": body.decision,
        },
        "error": None,
    }
