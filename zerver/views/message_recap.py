from django.conf import settings
from django.http import HttpRequest, HttpResponse
from django.utils.translation import gettext as _

from analytics.lib.counts import COUNT_STATS
from zerver.lib.exceptions import JsonableError
from zerver.lib.queue import queue_event_on_commit
from zerver.lib.response import json_success
from zerver.lib.typed_endpoint import typed_endpoint_without_parameters
from zerver.models import UserProfile


@typed_endpoint_without_parameters
def generate_messages_recap(
    request: HttpRequest,
    user_profile: UserProfile,
) -> HttpResponse:
    if settings.TOPIC_SUMMARIZATION_MODEL is None:  # nocoverage
        raise JsonableError(_("AI features are not enabled on this server."))

    if not user_profile.can_summarize_topics():
        raise JsonableError(_("Insufficient permission"))

    if settings.MAX_PER_USER_MONTHLY_AI_COST is not None:
        used_credits = COUNT_STATS["ai_credit_usage::day"].current_month_accumulated_count_for_user(
            user_profile
        )
        if used_credits >= settings.MAX_PER_USER_MONTHLY_AI_COST * 1000000000:
            raise JsonableError(_("Reached monthly limit for AI credits."))

    queue_event_on_commit(
        "message_recap",
        {"user_profile_id": user_profile.id, "realm_id": user_profile.realm_id},
    )

    return json_success(request)
