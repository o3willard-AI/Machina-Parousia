"""Onboarding logic for free agent accounts."""

from pydantic import BaseModel, Field
from fastapi import HTTPException
from parousia.auth.accounts import AccountStore


class OnboardRequest(BaseModel):
    account_id: str = Field(..., min_length=3, max_length=64, pattern=r"^[a-z0-9][a-z0-9_-]*$")
    email: str = Field(default="")
    display_name: str = Field(default="")


class OnboardResponse(BaseModel):
    account_id: str
    api_key: str
    tier: str
    message: str


def handle_onboard(store: AccountStore, request: OnboardRequest) -> OnboardResponse:
    """Create a free-tier account. Returns the API key once."""
    if store.account_exists(request.account_id):
        raise HTTPException(status_code=409, detail=f"Account '{request.account_id}' already exists")

    account, api_key = store.create_account(
        account_id=request.account_id,
        tier="free",
        email=request.email,
        display_name=request.display_name,
    )

    return OnboardResponse(
        account_id=account.account_id,
        api_key=api_key,
        tier=account.tier,
        message=(
            "Account created! Save your API key — it will not be shown again. "
            "Use it in the Authorization header: 'Bearer <your_key>'"
        ),
    )
