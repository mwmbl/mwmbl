"""
Custom authentication for ninja-jwt.
"""


def require_email_confirmation(user):
    # This function is used in settings, so we need to import here
    from allauth.account.utils import has_verified_email

    if not user.is_active:
        return False

    return has_verified_email(user)


class UsernameOrEmailBackend:
    """
    Auth backend that accepts either username or email in the username field.
    Used by the JWT token endpoint, which always passes credentials as username+password.
    """

    def authenticate(self, request, username=None, password=None, **kwargs):
        from django.contrib.auth import get_user_model
        UserModel = get_user_model()

        if not username or not password:
            return None

        try:
            user = UserModel.objects.get(username=username)
        except UserModel.DoesNotExist:
            try:
                user = UserModel.objects.get(email=username)
            except UserModel.DoesNotExist:
                return None

        if user.check_password(password):
            return user
        return None

    def get_user(self, user_id):
        from django.contrib.auth import get_user_model
        UserModel = get_user_model()
        try:
            return UserModel.objects.get(pk=user_id)
        except UserModel.DoesNotExist:
            return None
