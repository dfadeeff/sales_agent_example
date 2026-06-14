from pydantic import BaseModel, EmailStr, field_validator


class RegisterRequest(BaseModel):
    email: str
    password: str
    first_name: str
    last_name: str
    company: str = ""
    address: str = ""
    city: str = ""
    state: str = ""
    country: str = ""
    postal_code: str = ""
    phone: str = ""
    fax: str = ""

    @field_validator("password")
    @classmethod
    def password_min_length(cls, v: str) -> str:
        if len(v) < 6:
            raise ValueError("Password must be at least 6 characters")
        return v


class LoginRequest(BaseModel):
    email: str
    password: str


class ChatRequest(BaseModel):
    message: str


class AdminDecisionRequest(BaseModel):
    decision: str  # "approved" or "denied"
    denial_reason: str = ""

    @field_validator("decision")
    @classmethod
    def valid_decision(cls, v: str) -> str:
        if v not in ("approved", "denied"):
            raise ValueError("decision must be 'approved' or 'denied'")
        return v
