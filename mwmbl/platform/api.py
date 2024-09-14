from ninja_extra import NinjaExtraAPI
from ninja_jwt.controller import NinjaJWTDefaultController


api = NinjaExtraAPI(urls_namespace="platform")
api.register_controllers(NinjaJWTDefaultController)

