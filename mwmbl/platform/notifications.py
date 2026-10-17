"""v2 notification endpoints: messages a user sees until they dismiss them."""

from django.utils import timezone
from ninja import Router
from ninja_jwt.authentication import JWTAuth

from mwmbl.exceptions import InvalidRequest
from mwmbl.models import UserNotification
from mwmbl.platform.schemas import UserNotificationResponse

router = Router(tags=["Notifications"])


@router.get(
    "/",
    auth=JWTAuth(),
    response=list[UserNotificationResponse],
    summary="List notifications",
    description="Returns the user's notifications that they haven't dismissed, oldest first.",
)
def list_notifications(request):
    return UserNotification.objects.filter(user=request.user, dismissed_at__isnull=True).order_by("created")


@router.post(
    "/{notification_id}/dismiss",
    auth=JWTAuth(),
    response={204: None},
    summary="Dismiss a notification",
    description="Marks one of the user's notifications as dismissed, so it is no longer listed. Dismissing a "
    "notification that is already dismissed does nothing. Returns 404 if the notification does not exist or "
    "belongs to another user.",
)
def dismiss_notification(request, notification_id: int):
    notifications = UserNotification.objects.filter(user=request.user, id=notification_id)
    if not notifications.exists():
        raise InvalidRequest("Notification not found.", status=404)
    notifications.filter(dismissed_at__isnull=True).update(dismissed_at=timezone.now())
    return 204, None
