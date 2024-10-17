from allauth.account.adapter import get_adapter
from allauth.account.models import EmailConfirmationHMAC
from allauth.account.utils import setup_user_email, send_email_confirmation
from ninja.pagination import paginate
from ninja_extra import NinjaExtraAPI
from ninja_jwt.authentication import JWTAuth
from ninja_jwt.controller import NinjaJWTDefaultController

from mwmbl.models import MwmblUser, DomainSubmission, UpdateDomainSubmission
from mwmbl.platform.schemas import Registration, ConfirmEmail

api = NinjaExtraAPI(urls_namespace="platform")
api.register_controllers(NinjaJWTDefaultController)


class InvalidRequest(Exception):
    def __init__(self, message: str, status: int = 403):
        self.message = message
        self.status = status


@api.exception_handler(InvalidRequest)
def no_permission(request, exc: InvalidRequest):
    return api.create_response(
        request,
        {"status": "error", "message": exc.message},
        status=exc.status,
    )


def check_email_verified(request):
    from_email_address = request.user.emailaddress_set.first()
    if not from_email_address.verified:
        raise InvalidRequest("Email address is not verified")


@api.post('/register')
def register(request, registration: Registration):
    # Check for existing user with this username
    if MwmblUser.objects.filter(username=registration.username).exists():
        raise InvalidRequest("Username already exists")

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
        raise InvalidRequest("Invalid confirmation key")

    # Check the signed email address matches this one
    if confirmation.email_address.email != confirm.email:
        raise InvalidRequest("Invalid username or email")

    # Check the username matches
    user = MwmblUser.objects.get(username=confirm.username)
    if user.email != confirm.email:
        raise InvalidRequest("Invalid username or email")

    adapter = get_adapter()
    adapter.confirm_email(request, confirmation.email_address)

    return {
        "status": "ok",
        "username": confirm.username,
        "message": "Email confirmed successfully."
    }


@api.get("/protected", auth=JWTAuth())
def protected(request):
    check_email_verified(request)
    return {"status": "ok", "message": "You are authenticated!"}


@api.delete("/users/{username}", auth=JWTAuth())
def delete_user(request, username: str):
    user = MwmblUser.objects.get(username=username)
    if user is None:
        raise InvalidRequest("User not found.", status=404)

    if user != request.user:
        raise InvalidRequest("You can only delete your own account.")

    user.delete()
    return {"status": "ok", "message": "User deleted."}


@api.get("/domain-submissions/domains/{domain}", response=list[DomainSubmission])
@paginate
def get_domain_submissions_for_domain(request, domain: str) -> list[DomainSubmission]:
    return DomainSubmission.objects.filter(name=domain).all()


@api.get("/domain-submissions", response=list[DomainSubmission])
@paginate
def get_domain_submissions(request) -> list[DomainSubmission]:
    return DomainSubmission.objects.all()


@api.post("/app/domain-submissions/", auth=JWTAuth())
def submit_domain(request, domain: str):
    check_email_verified(request)
    submission = DomainSubmission(name=domain, submitted_by=request.user)
    submission.save()
    return {"status": "ok", "message": "Domain submitted for review."}


@api.delete("app/domain-submissions/ids/{submission_id}", auth=JWTAuth())
def delete_submission(request, submission_id: int):
    check_email_verified(request)
    submission = DomainSubmission.objects.get(id=submission_id)
    if submission is None:
        raise InvalidRequest("Submission not found.", status=404)

    if request.user != submission.submitted_by:
        raise InvalidRequest("You can only delete your own submissions.")

    submission.delete()
    return {"status": "ok", "message": "Submission deleted."}


@api.post("app/domain-submissions/ids/{submission_id}", auth=JWTAuth())
def update_submission_status(request, submission_id: int, update_submission: UpdateDomainSubmission):
    check_email_verified(request)
    submission = DomainSubmission.objects.get(id=submission_id)
    if submission is None:
        raise InvalidRequest("Submission not found.", status=404)

    # Can only update if the user has the permission
    if not request.user.has_perm("change_domain_submission_status"):
        raise InvalidRequest("You do not have permission to update this submission.")

    submission.status = update_submission.status
    submission.rejection_reason = update_submission.rejection_reason
    submission.rejection_detail = update_submission.rejection_detail
    submission.save()
    return {"status": "ok", "message": "Submission updated."}
