import hashlib
import logging
from collections.abc import Mapping
from typing import Any

from django.conf import settings
from django.db import transaction
from django.utils.timezone import now as timezone_now
from openai import OpenAI
from typing_extensions import override

from analytics.lib.counts import COUNT_STATS, do_increment_logging_stat
from zerver.actions.message_summary import format_zulip_messages_for_model, make_message
from zerver.lib.cache import KEY_PREFIX, get_cache_backend
from zerver.lib.event_types import TopicDriftSuggestionEvent
from zerver.lib.message import messages_for_ids
from zerver.lib.topic import participants_for_topic
from zerver.models import Message, UserProfile
from zerver.models.realms import MessageEditHistoryVisibilityPolicyEnum
from zerver.tornado.django_api import send_event_on_commit
from zerver.worker.base import QueueProcessingWorker, assign_queue

logger = logging.getLogger(__name__)

# Only run the (expensive) drift check once a topic has accumulated this
# many messages since the previous check. We use the topic's total message
# count as a stateless proxy for "since the previous check": this fires
# once at every multiple of N without needing to persist a per-topic
# last-checked counter.
MESSAGES_BETWEEN_DRIFT_CHECKS = 5

# How many of the topic's most recent messages to send the model when
# checking for drift.
MAX_MESSAGES_FOR_DRIFT_CHECK = 20

NO_DRIFT_SENTINEL = "NO_DRIFT"

# How long a claimed (topic, message-count boundary) pair blocks duplicate
# checks. Just needs to outlast how long a single drift check can take
# (including LLM retries), not the topic's lifetime.
DRIFT_CHECK_CLAIM_TIMEOUT_SECONDS = 600


def _topic_message_ids(message: Message, limit: int | None = None) -> list[int]:
    query = Message.objects.filter(
        # Uses index: zerver_message_realm_recipient_upper_subject
        realm_id=message.realm_id,
        recipient_id=message.recipient_id,
        subject__iexact=message.topic_name(),
        is_channel_message=True,
    ).order_by("-id")
    if limit is not None:
        query = query[:limit]
    return list(query.values_list("id", flat=True))


def _claim_drift_check(recipient_id: int, topic_name: str, message_count: int) -> bool:
    # When several messages land in the same topic close together (e.g. a
    # bulk import), their jobs can all observe the same
    # MESSAGES_BETWEEN_DRIFT_CHECKS boundary once every message has been
    # committed, and would otherwise all pay for their own LLM call for
    # what should be a single check. memcached's atomic add-if-absent
    # lets only the first job through; the id/topic/count triple
    # identifies that boundary uniquely.
    raw_key = f"{recipient_id}:{topic_name}:{message_count}"
    key_hash = hashlib.sha256(raw_key.encode()).hexdigest()
    cache_backend = get_cache_backend(None)
    return cache_backend.add(
        KEY_PREFIX + f"topic_drift_check:{key_hash}",
        True,
        timeout=DRIFT_CHECK_CLAIM_TIMEOUT_SECONDS,
    )


@assign_queue("topic_drift")
class TopicDriftDetector(QueueProcessingWorker):
    @override
    def consume(self, event: Mapping[str, Any]) -> None:
        try:
            message = Message.objects.get(id=event["message_id"])
        except Message.DoesNotExist:
            # Message may have been deleted.
            return

        if message.topic_name() != event["topic_name"] or not message.is_channel_message:
            # The message was moved to another topic, or edited into a
            # direct message, before we got to it; the drift check we were
            # asked to do no longer applies.
            return

        total_messages_in_topic = len(_topic_message_ids(message))
        if total_messages_in_topic % MESSAGES_BETWEEN_DRIFT_CHECKS != 0:
            return

        if not _claim_drift_check(
            message.recipient_id, message.topic_name(), total_messages_in_topic
        ):
            # Another job already claimed this topic's check at this
            # message-count boundary.
            return

        sender = message.sender
        if settings.TOPIC_SUMMARIZATION_MODEL is None:
            return
        if not sender.can_summarize_topics():
            return
        if settings.MAX_PER_USER_MONTHLY_AI_COST is not None:
            used_credits = COUNT_STATS[
                "ai_credit_usage::day"
            ].current_month_accumulated_count_for_user(sender)
            if used_credits >= settings.MAX_PER_USER_MONTHLY_AI_COST * 1000000000:
                return

        suggested_topic_name = self._check_for_drift(sender, message)
        if suggested_topic_name is None:
            return

        # Re-fetch and re-validate immediately before sending the event,
        # in case the topic changed while the (slow) LLM call was in
        # flight, and keep that check inside the same transaction as the
        # send so the two can't race with a concurrent topic move.
        #
        # Ideally, we should use `durable=True` here. However, as in
        # embed_links.py, this function isn't always called as the
        # outermost transaction in tests, where `consume` is invoked
        # directly rather than via the queue.
        with transaction.atomic(savepoint=False):
            try:
                message = Message.objects.select_for_update(no_key=True).get(id=event["message_id"])
            except Message.DoesNotExist:
                return
            if message.topic_name() != event["topic_name"] or not message.is_channel_message:
                return

            realm = message.realm
            participant_ids = participants_for_topic(
                realm.id, message.recipient_id, message.topic_name()
            )
            event_to_send = TopicDriftSuggestionEvent(
                message_id=message.id,
                stream_id=event["stream_id"],
                topic_name=message.topic_name(),
                suggested_topic_name=suggested_topic_name,
            )
            send_event_on_commit(realm, event_to_send, list(participant_ids))

    def _check_for_drift(self, sender: UserProfile, message: Message) -> str | None:
        message_ids = _topic_message_ids(message, limit=MAX_MESSAGES_FOR_DRIFT_CHECK)
        message_ids.reverse()

        user_message_flags: dict[int, list[str]] = {message_id: [] for message_id in message_ids}
        message_list = messages_for_ids(
            message_ids=message_ids,
            user_message_flags=user_message_flags,
            search_fields={},
            apply_markdown=False,
            client_gravatar=True,
            allow_empty_topic_name=True,
            message_edit_history_visibility_policy=MessageEditHistoryVisibilityPolicyEnum.none.value,
            user_profile=sender,
            realm=sender.realm,
        )
        formatted_conversation = format_zulip_messages_for_model(message_list)

        intro = (
            "The following is a chat conversation in the Zulip team chat app, "
            f'currently titled "{message.topic_name()}".'
        )
        prompt = (
            "Decide whether this conversation has sustainedly drifted away "
            "from its current title and would be better served by a new, "
            "more accurate title. Only say it has drifted if the majority "
            "of the most recent messages, not just the latest one or two, "
            "have consistently moved on to a different subject; a brief "
            "aside or a single off-topic message does not count as drift, "
            f'and you should prefer "{NO_DRIFT_SENTINEL}" when unsure. If '
            f'the current title still fits, respond with exactly the '
            f'single word "{NO_DRIFT_SENTINEL}" and nothing else. '
            "Otherwise, respond with ONLY a concise replacement title (a "
            "few words, no punctuation, no quotes, no explanation)."
        )
        messages = [
            make_message(intro, "system"),
            make_message(formatted_conversation),
            make_message(prompt),
        ]

        client = OpenAI(
            api_key=settings.TOPIC_SUMMARIZATION_API_KEY,
            base_url=settings.TOPIC_SUMMARIZATION_API_BASE,
            # More generous than the default (2), since this worker has
            # observed transient connection errors to the LLM provider.
            max_retries=5,
        )
        response = client.chat.completions.create(
            model=settings.TOPIC_SUMMARIZATION_MODEL,
            messages=messages,
            **settings.TOPIC_SUMMARIZATION_PARAMETERS,
        )
        assert response.usage is not None
        credits_used = (
            response.usage.completion_tokens * settings.OUTPUT_COST_PER_GIGATOKEN
            + response.usage.prompt_tokens * settings.INPUT_COST_PER_GIGATOKEN
        )
        do_increment_logging_stat(
            sender,
            COUNT_STATS["ai_credit_usage::day"],
            None,
            timezone_now(),
            credits_used,
        )

        suggestion = response.choices[0].message.content
        assert suggestion is not None
        suggestion = suggestion.strip().strip('"')
        if not suggestion or suggestion.upper() == NO_DRIFT_SENTINEL:
            return None
        return suggestion
