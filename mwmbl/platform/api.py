from allauth.account.adapter import get_adapter
from allauth.account.models import EmailConfirmationHMAC
from allauth.account.utils import setup_user_email, send_email_confirmation
from ninja_extra import NinjaExtraAPI, http_post
from ninja_jwt.authentication import JWTAuth
from ninja_jwt.controller import NinjaJWTDefaultController, schema
from ninja_jwt.tokens import RefreshToken

from mwmbl.models import MwmblUser
from mwmbl.platform.schemas import Registration, ConfirmEmail

api = NinjaExtraAPI(urls_namespace="platform")
api.register_controllers(NinjaJWTDefaultController)


@api.post('/register')
def register(request, registration: Registration):
    # Check for existing user with this username
    if MwmblUser.objects.filter(username=registration.username).exists():
        return {"status": "error", "message": "Username already exists"}

    user = MwmblUser(username=registration.username, email=registration.email)
    user.set_password(registration.password)
    user.save()

    setup_user_email(request, user, [])
    send_email_confirmation(request, user, signup=True)

    return {
        "status": "ok",
        "username": registration.username,
        "message": "User registered successfully. Check your email for confirmation."
    }


@api.post("/confirm-email")
def confirm_email(request, confirm: ConfirmEmail):
    confirmation = EmailConfirmationHMAC.from_key(confirm.key)
    if confirmation is None:
        return {"status": "error", "message": "Invalid key"}

    # Check the signed email address matches this one
    if confirmation.email_address.email != confirm.email:
        return {"status": "error", "message": "Email address does not match"}

    # Check the username matches
    user = MwmblUser.objects.get(username=confirm.username)
    if user.email != confirm.email:
        return {"status": "error", "message": "User email does not match"}

    adapter = get_adapter()
    adapter.confirm_email(request, confirmation.email_address)

    return {
        "status": "ok",
        "username": confirm.username,
        "message": "Email confirmed successfully."
    }


@api.get("/protected", auth=JWTAuth())
def protected(request):
    return {"status": "ok", "message": "You are authenticated!"}
