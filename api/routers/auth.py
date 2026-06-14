from typing import Annotated

import aiosqlite
from fastapi import APIRouter, Depends, HTTPException, status

from api.auth import create_token, hash_password, verify_password
from api.db import get_db
from api.models import LoginRequest, RegisterRequest

router = APIRouter(prefix="/auth", tags=["auth"])


@router.post("/register", status_code=201)
async def register(
    body: RegisterRequest,
    db: Annotated[aiosqlite.Connection, Depends(get_db)],
) -> dict:
    # Check email uniqueness
    async with db.execute(
        "SELECT user_id FROM app_users WHERE email = ?", (body.email,)
    ) as cur:
        if await cur.fetchone():
            raise HTTPException(status_code=400, detail="Email already registered")

    # Insert Customer row
    async with db.execute(
        """INSERT INTO Customer
           (FirstName, LastName, Company, Address, City, State, Country,
            PostalCode, Phone, Fax, Email)
           VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
        (
            body.first_name, body.last_name, body.company,
            body.address, body.city, body.state, body.country,
            body.postal_code, body.phone, body.fax, body.email,
        ),
    ) as cur:
        customer_id = cur.lastrowid

    # Insert app_users row
    ph = hash_password(body.password)
    async with db.execute(
        "INSERT INTO app_users (customer_id, email, password_hash, role) "
        "VALUES (?, ?, ?, 'customer')",
        (customer_id, body.email, ph),
    ) as cur:
        user_id = cur.lastrowid

    await db.commit()

    token = create_token(user_id, "customer", customer_id)
    return {"data": {"user_id": user_id, "customer_id": customer_id, "token": token}, "error": None}


@router.post("/login")
async def login(
    body: LoginRequest,
    db: Annotated[aiosqlite.Connection, Depends(get_db)],
) -> dict:
    async with db.execute(
        "SELECT user_id, password_hash, role, customer_id FROM app_users WHERE email = ?",
        (body.email,),
    ) as cur:
        row = await cur.fetchone()

    if not row or not verify_password(body.password, row["password_hash"]):
        raise HTTPException(status_code=401, detail="Invalid email or password")

    token = create_token(row["user_id"], row["role"], row["customer_id"])
    return {
        "data": {
            "token": token,
            "role": row["role"],
            "customer_id": row["customer_id"],
        },
        "error": None,
    }
