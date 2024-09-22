"""
Custom authentication for ninja-jwt.
"""


def require_email_confirmation(user):
    # This function is used in settings, so we need to import here
    from allauth.account.utils import has_verified_email

    if not user.is_active:
        return False

    return has_verified_email(user)
