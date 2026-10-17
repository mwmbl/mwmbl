"""v2 membership endpoints: paid and volunteer memberships together, so the membership page needs one call."""

from ninja import Router
from ninja_jwt.authentication import JWTAuth

from mwmbl import membership
from mwmbl.models import Membership
from mwmbl.platform.api import get_combined_search_usage, list_membership_tiers
from mwmbl.platform.schemas import (
    MembershipTiersV2Response,
    MembershipV2Response,
    PaidMembershipResponse,
    VolunteerMembershipResponse,
    VolunteerTierResponse,
)
from mwmbl.volunteer_membership import current_volunteer_membership

router = Router(tags=["Membership"])


@router.get(
    "/",
    auth=JWTAuth(),
    response=MembershipV2Response,
    summary="Get membership",
    description="Returns the user's paid membership and their highest current volunteer tier, each null if "
    "they don't hold one, and their Combined Search usage and limit this month. Cancelling or changing a "
    "paid membership is done through /api/v1/platform/membership/.",
)
def get_membership(request):
    paid = Membership.objects.filter(user=request.user).first()
    volunteer = current_volunteer_membership(request.user.id)
    return MembershipV2Response(
        paid=PaidMembershipResponse(
            tier=paid.tier,
            name=paid.get_tier_display(),
            current_period_end=paid.current_period_end,
            cancel_at_period_end=paid.cancel_at_period_end,
        )
        if paid
        else None,
        volunteer=VolunteerMembershipResponse(
            tier=volunteer.tier,
            name=volunteer.get_tier_display(),
            valid_until=volunteer.valid_until,
        )
        if volunteer
        else None,
        combined_search=get_combined_search_usage(request),
    )


@router.get(
    "/tiers",
    response=MembershipTiersV2Response,
    summary="List membership tiers",
    description="Returns the paid tiers with their monthly prices and perks, and the volunteer tiers, which are "
    "earned by crawling rather than bought, with what earns each one and its perks.",
)
def list_tiers(request):
    return MembershipTiersV2Response(
        paid=list_membership_tiers(request),
        volunteer=[
            VolunteerTierResponse(
                tier=info.tier,
                name=info.tier.label,
                days_per_month=info.days_per_month,
                min_results_per_day=info.min_results_per_day,
                perks=info.perks,
            )
            for info in membership.VOLUNTEER_TIERS
        ],
    )
