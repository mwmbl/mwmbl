from ninja_extra import NinjaExtraAPI
from ninja_jwt.controller import NinjaJWTDefaultController
from ninja_jwt.tokens import RefreshToken

from mwmbl.models import MwmblUser
from mwmbl.platform.schemas import Registration

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

    refresh = RefreshToken.for_user(user)

    return {
        "status": "ok",
        "username": registration.username,
        'refresh': str(refresh),
        'access': str(refresh.access_token),
    }
