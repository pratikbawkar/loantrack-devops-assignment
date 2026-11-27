"""SQL for the loans table. Queries are parameterised; status is always set by the server."""

from __future__ import annotations

from typing import Any

from psycopg.rows import dict_row

from .db import Database
from .schemas import LoanCreate

_COLUMNS = "id, borrower_name, loan_amount, property_city, status, created_at"

LIST_LOANS_SQL = f"SELECT {_COLUMNS} FROM loans ORDER BY id DESC"

# 'PENDING' is a literal here on purpose: the client can never choose the status.
INSERT_LOAN_SQL = (
    "INSERT INTO loans (borrower_name, loan_amount, property_city, status) "
    "VALUES (%(borrower_name)s, %(loan_amount)s, %(property_city)s, 'PENDING') "
    f"RETURNING {_COLUMNS}"
)


async def list_loans(db: Database) -> list[dict[str, Any]]:
    async with db.connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(LIST_LOANS_SQL)
            return await cur.fetchall()


async def create_loan(db: Database, loan: LoanCreate) -> dict[str, Any]:
    params = {
        "borrower_name": loan.borrower_name,
        "loan_amount": loan.loan_amount,
        "property_city": loan.property_city,
    }
    async with db.connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(INSERT_LOAN_SQL, params)
            row = await cur.fetchone()
    # The pool's connection context commits on successful exit.
    return row
