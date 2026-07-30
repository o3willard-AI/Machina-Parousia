"""Onboarding logic for free agent accounts — invite-key gated."""

from pydantic import BaseModel, Field
from fastapi import HTTPException
from parousia.auth.accounts import AccountStore
from parousia.auth.invites import InviteStore


class OnboardRequest(BaseModel):
    account_id: str = Field(..., min_length=3, max_length=64, pattern=r"^[a-z0-9][a-z0-9_-]*$")
    invite_code: str = Field(..., min_length=10, description="One-time invite key from your sponsor")
    email: str = Field(default="")
    display_name: str = Field(default="")


class OnboardResponse(BaseModel):
    account_id: str
    api_key: str
    tier: str
    message: str


def handle_onboard(
    store: AccountStore,
    invite_store: InviteStore,
    request: OnboardRequest,
) -> OnboardResponse:
    """Create a free-tier account, validated by a one-time invite key.

    The invite key is consumed on success — it cannot be reused.
    """
    # 1. Validate invite key
    ok, reason = invite_store.validate(request.invite_code)
    if not ok:
        raise HTTPException(status_code=400, detail=f"Invalid invite code: {reason}")

    # 2. Check for duplicate account
    if store.account_exists(request.account_id):
        raise HTTPException(status_code=409, detail=f"Account '{request.account_id}' already exists")

    # 3. Create account
    account, api_key = store.create_account(
        account_id=request.account_id,
        tier="free",
        email=request.email,
        display_name=request.display_name,
    )

    # 4. Consume the invite key
    invite_store.consume(request.invite_code, request.account_id)

    return OnboardResponse(
        account_id=account.account_id,
        api_key=api_key,
        tier=account.tier,
        message=(
            "Account created! Save your API key — it will not be shown again. "
            "Use it in the Authorization header: 'Bearer <your_key>'"
        ),
    )
