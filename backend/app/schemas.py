"""Request and response models for the loans API."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field, field_serializer, field_validator


def _reject_nul(value: str | None) -> str | None:
    # PostgreSQL text cannot hold NUL; without this it fails in the database as a 500.
    # (Lone surrogates need no check: Pydantic's str validation already rejects
    # them with a 422 of type "string_unicode".)
    if value is not None and "\x00" in value:
        raise ValueError("must not contain NUL (\\u0000) characters")
    return value


class LoanCreate(BaseModel):
    """POST /loans body. `status` is NOT accepted: extra fields are rejected with 422.
    The backend always stores PENDING."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    borrower_name: str = Field(min_length=1, max_length=200)
    # NUMERIC(14,2): at most 14 digits, 2 after the decimal point -> max 999999999999.99
    loan_amount: Decimal = Field(gt=0, max_digits=14, decimal_places=2)
    property_city: str | None = Field(default=None, max_length=100)

    @field_validator("borrower_name", "property_city")
    @classmethod
    def no_nul_characters(cls, value: str | None) -> str | None:
        return _reject_nul(value)

    @field_validator("property_city")
    @classmethod
    def empty_city_is_null(cls, value: str | None) -> str | None:
        # Already stripped by str_strip_whitespace; an empty form field means "no city".
        return value or None


class Loan(BaseModel):
    """One row of the loans table as returned by the API."""

    id: int
    borrower_name: str
    loan_amount: Decimal
    property_city: str | None
    status: str
    created_at: datetime

    @field_serializer("loan_amount")
    def amount_as_string(self, value: Decimal) -> str:
        # String keeps exact precision (JSON numbers are floats in JavaScript).
        return f"{value:.2f}"
