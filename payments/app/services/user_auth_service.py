import uuid

from app.models.schemas import UserAuthorizationRequest, UserAuthorizationResult


async def authorize_user(request: UserAuthorizationRequest) -> UserAuthorizationResult:
    # WHY: this is a rubber-stamp verifier — every proof passes. It stands in
    # for the real IdP / 3DS integration point. The consent RECORD is real and
    # auditable; the verification itself is mocked for the hackathon.
    # NOTE: denial is currently unreachable here by design; the 401 path in the
    # router exists for when a real verifier can return authorized=False.
    return UserAuthorizationResult(
        authorized=True,
        authorization_id=str(uuid.uuid4()),
        method=request.method,
        preview_id=request.preview_id,
    )
