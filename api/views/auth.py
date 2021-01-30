import uuid
import logging
import datetime as dt

from fastapi import APIRouter, Depends
from fastapi.security import OAuth2PasswordRequestForm
from pydantic import UUID4

from api import db
from api.depends import (
    AUTH_RESPONSES,
    get_session,
    anonymous_required,
    login_required,
    get_auth_token,
)
from api.environment import settings
from api.exceptions import (
    APIException,
    CredentialsException,
    BannedUserException,
    NotFoundException,
)
from api.models import User, UserRevokedToken
from api.services.user import access_token_for_user
from api.schemas import DetailResponse, auth as schema
from api.schemas.user import UserEmailIn, UserSetPasswordIn
from api.utils.auth import verify_password, generate_password_hash
from api.utils.email import send_message


logger = logging.getLogger(__name__)

router = APIRouter()


@router.post(
    "/token",
    response_model=schema.AuthTokenOut,
    responses=AUTH_RESPONSES,
)
def log_in(
    form_data: OAuth2PasswordRequestForm = Depends(),
    session: db.Session = Depends(get_session),
    _=Depends(anonymous_required),
):
    """Log a user in and return a JWT authentication token to authenticate future requests.

    **Please note:** Only username and password are currently in use, and `username` must be the
    user's registered email. You can ignore the other form fields.
    """
    email = form_data.username.lower()
    user = session.query(User).filter(User.email == email).first()
    if not user or not verify_password(form_data.password, user.password):
        raise CredentialsException(
            detail="Incorrect username or password",
        )
    if user.is_banned:
        raise BannedUserException()
    access_token = access_token_for_user(user)
    return {"access_token": access_token, "token_type": "bearer", "user": user}


@router.delete("/token", response_model=DetailResponse, responses=AUTH_RESPONSES)
def log_out(
    session: db.Session = Depends(get_session),
    jwt_payload: dict = Depends(get_auth_token),
    current_user: "User" = Depends(login_required),
):
    """Log a user out and revoke their JWT token's access rights.

    It's a good idea to invoke this whenever an authenticated user logs out, because tokens can otherwise be quite
    long-lived.
    """
    expires_at = dt.datetime.fromtimestamp(jwt_payload["exp"], tz=dt.timezone.utc)
    # No need to do `.get("jti")` here because a missing JTI would result in a credentials error in the dependencies
    revoked_hex = jwt_payload["jti"]
    revoked_uuid = uuid.UUID(hex=revoked_hex)
    revoked_token = UserRevokedToken(
        revoked_uuid=revoked_uuid, user_id=current_user.id, expires=expires_at
    )
    session.add(revoked_token)
    session.commit()
    return {"detail": "Token successfully revoked."}


@router.post(
    "/reset",
    response_model=DetailResponse,
    responses={
        404: {"model": DetailResponse, "description": "Email has not been registered."},
        **AUTH_RESPONSES,
    },
)
def request_password_reset(
    data: UserEmailIn,
    session: db.Session = Depends(get_session),
    _=Depends(anonymous_required),
):
    """Request a reset password link for the given email."""
    email = data.email.lower()
    user: User = session.query(User).filter(User.email == email).first()
    if not user:
        raise NotFoundException(detail="No account found for email.")
    if user.is_banned:
        raise BannedUserException()
    user.reset_uuid = uuid.uuid4()
    session.commit()
    if not send_message(
        recipient=user.email,
        template_id=settings.sendgrid_reset_template,
        data={"reset_token": user.reset_uuid, "email": user.email},
    ):
        if settings.debug:
            logger.debug(f"RESET TOKEN FOR {email}: {user.reset_uuid}")
        raise APIException(
            detail="Unable to send password reset email; please contact the site owner."
        )
    return {"detail": "A link to reset your password has been sent to your email!"}


@router.post(
    "/reset/{token}",
    response_model=schema.AuthTokenOut,
    responses={
        404: {"model": DetailResponse, "description": "Bad invitation token"},
        **AUTH_RESPONSES,
    },
)
def reset_password(
    token: UUID4,
    data: UserSetPasswordIn,
    session: db.Session = Depends(get_session),
    _=Depends(anonymous_required),
):
    """Reset the password for account associated with the given reset token."""
    user = session.query(User).filter(User.reset_uuid == token).first()
    if user is None:
        raise NotFoundException(
            detail="Token not found. Please request a new password reset."
        )
    user.password = generate_password_hash(data.password)
    user.reset_uuid = None
    session.commit()
    access_token = access_token_for_user(user)
    return {"access_token": access_token, "token_type": "bearer"}
