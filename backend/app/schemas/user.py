from pydantic import BaseModel, EmailStr, Field

class RegisterRequest(BaseModel):
    name: str
    email: EmailStr
    password: str

class LoginRequest(BaseModel):
    email: EmailStr
    password: str

class UserResponse(BaseModel):
    id: str
    name: str
    email: str
    avatar: str | None
    soul_coin_balance: int
    is_admin: bool = False
    wallet_address: str | None = None
    # Feature flags the frontend needs to gate UI entries (e.g. hide the Lab
    # button when the deploy has LAB_ENABLED=false).
    lab_enabled: bool = False

class AuthResponse(BaseModel):
    access_token: str
    user: UserResponse


class WalletChallengeRequest(BaseModel):
    address: str = Field(min_length=42, max_length=42)
    chain_id: int = Field(gt=0)


class WalletChallengeResponse(BaseModel):
    message: str
    nonce: str
    expires_at: str
    chain_id: int
    chain_name: str


class WalletVerifyRequest(BaseModel):
    address: str = Field(min_length=42, max_length=42)
    message: str = Field(min_length=1, max_length=4096)
    signature: str = Field(min_length=2, max_length=1024)
    nonce: str = Field(min_length=8, max_length=128, pattern=r"^[A-Za-z0-9]+$")
    chain_id: int = Field(gt=0)
