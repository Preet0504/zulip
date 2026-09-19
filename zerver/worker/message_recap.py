import hashlib
import logging
from collections.abc import Mapping
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from django.conf import settings
from django.db import connections
from django.utils.timezone import now as timezone_now
from openai import OpenAI
from typing_extensions import override

from analytics.lib.counts import COUNT_STATS, do_increment_logging_stat
from zerver.actions.message_recap import RecapConversation, get_unread_conversations_for_recap
from zerver.actions.message_summary import format_zulip_messages_for_model, make_message
from zerver.lib.cache import cache_get, cache_set
from zerver.lib.event_types import MessageRecapReadyEvent, RecapConversationRef
from zerver.lib.markdown import markdown_convert
from zerver.lib.message import messages_for_ids
from zerver.models import Realm, UserProfile
from zerver.models.realms import MessageEditHistoryVisibilityPolicyEnum
from zerver.tornado.django_api import send_event_on_commit
from zerver.worker.base import QueueProcessingWorker, assign_queue

logger = logging.getLogger(__name__)

# Below this many unread messages, skip the LLM entirely and just show the
# raw message(s) in the recap.
MIN_MESSAGES_FOR_SUMMARY = 3

# Cover this fraction of each conversation's unread messages...
COVERAGE_FRACTION = 0.5
# ...but never more than this many, regardless of conversation size (safety
# ceiling, in case a single conversation is unexpectedly large).
MAX_MESSAGES_PER_CONVERSATION = 50

# Cap how many conversations we summarize in parallel, to stay within the
# LLM provider's rate limits.
MAX_CONCURRENT_SUMMARY_CALLS = 5

# How long a per-conversation summary stays cached, so reopening the recap
# page before reading those messages doesn't re-pay for the same LLM call.
CONVERSATION_SUMMARY_CACHE_SECONDS = 60 * 60


def _conversation_key(conversation: RecapConversation) -> str:
    if conversation.conversation_type == "stream":
        base = f"stream:{conversation.stream_id}:{conversation.topic_name}"
    elif conversation.conversation_type == "pm":
        base = f"pm:{conversation.other_user_id}"
    else:
        base = f"huddle:{conversation.user_ids_string}"
    return base


def _conversation_cache_key(conversation: RecapConversation, message_ids: list[int]) -> str:
    ids_part = ",".join(map(str, message_ids))
    raw_key = f"{_conversation_key(conversation)}:{ids_part}"
    key_hash = hashlib.sha256(raw_key.encode()).hexdigest()
    return f"message_recap:conv:{key_hash}"


def _select_message_ids(conversation: RecapConversation) -> list[int]:
    all_ids = sorted(conversation.unread_message_ids, reverse=True)
    target_count = max(1, round(len(all_ids) * COVERAGE_FRACTION))
    capped_count = min(target_count, MAX_MESSAGES_PER_CONVERSATION)
    # Keep chronological order for the prompt, even though we selected from
    # the most recent end.
    return sorted(all_ids[:capped_count])


def _summarize_conversation(user_profile: UserProfile, conversation: RecapConversation) -> str:
    message_ids = _select_message_ids(conversation)
    cache_key = _conversation_cache_key(conversation, message_ids)
    cached = cache_get(cache_key)
    if cached is not None:
        return cached

    user_message_flags: dict[int, list[str]] = {message_id: [] for message_id in message_ids}
    message_list = messages_for_ids(
        message_ids=message_ids,
        user_message_flags=user_message_flags,
        search_fields={},
        apply_markdown=False,
        client_gravatar=True,
        allow_empty_topic_name=True,
        message_edit_history_visibility_policy=MessageEditHistoryVisibilityPolicyEnum.none.value,
        user_profile=user_profile,
        realm=user_profile.realm,
    )

    if conversation.conversation_type == "stream":
        label = f"#{conversation.stream_name} > {conversation.topic_name}"
    else:
        label = "direct message conversation"

    intro = f"The following is a chat conversation in the Zulip team chat app: {label}."
    formatted_conversation = format_zulip_messages_for_model(message_list)
    prompt = (
        "Succinctly summarize this conversation based only on the information "
        "provided, in 1-2 sentences, for someone who has not read it yet. "
        "Mention key conclusions and actions, if any. Don't use an intro phrase."
    )
    messages = [
        make_message(intro, "system"),
        make_message(formatted_conversation),
        make_message(prompt),
    ]

    # The view gates enqueueing this job on TOPIC_SUMMARIZATION_MODEL being
    # configured, but that isn't visible to mypy across the queue boundary.
    model = settings.TOPIC_SUMMARIZATION_MODEL
    assert model is not None

    client = OpenAI(
        api_key=settings.TOPIC_SUMMARIZATION_API_KEY,
        base_url=settings.TOPIC_SUMMARIZATION_API_BASE,
        # More generous than the default (2), since this worker has
        # observed transient connection errors to the LLM provider.
        max_retries=5,
    )
    response = client.chat.completions.create(
        model=model,
        messages=messages,
        **settings.TOPIC_SUMMARIZATION_PARAMETERS,
    )
    assert response.usage is not None
    credits_used = (
        response.usage.completion_tokens * settings.OUTPUT_COST_PER_GIGATOKEN
        + response.usage.prompt_tokens * settings.INPUT_COST_PER_GIGATOKEN
    )
    do_increment_logging_stat(
        user_profile,
        COUNT_STATS["ai_credit_usage::day"],
        None,
        timezone_now(),
        credits_used,
    )

    summary = response.choices[0].message.content
    assert summary is not None
    cache_set(cache_key, summary, timeout=CONVERSATION_SUMMARY_CACHE_SECONDS, pickled_tupled=False)
    return summary


def _merge_summaries(labeled_summaries: list[tuple[str, str]]) -> str:
    if not labeled_summaries:
        return "You're all caught up! No unread conversations to recap."

    joined = "\n\n".join(f"**{label}**: {summary}" for label, summary in labeled_summaries)
    if len(labeled_summaries) == 1:
        return joined

    model = settings.TOPIC_SUMMARIZATION_MODEL
    assert model is not None

    client = OpenAI(
        api_key=settings.TOPIC_SUMMARIZATION_API_KEY,
        base_url=settings.TOPIC_SUMMARIZATION_API_BASE,
        max_retries=5,
    )
    messages = [
        make_message(
            "You are combining per-conversation summaries from a Zulip team chat app "
            "into one cohesive recap for a user catching up on unread messages.",
            "system",
        ),
        make_message(joined),
        make_message(
            "Combine these into a single organized recap, grouped naturally, using "
            "Zulip's CommonMark-based Markdown formatting. Keep each conversation's "
            "key point intact; don't invent information not present above."
        ),
    ]
    response = client.chat.completions.create(
        model=model,
        messages=messages,
        **settings.TOPIC_SUMMARIZATION_PARAMETERS,
    )
    merged = response.choices[0].message.content
    assert merged is not None
    return merged


@assign_queue("message_recap")
class MessageRecapWorker(QueueProcessingWorker):
    @override
    def consume(self, event: Mapping[str, Any]) -> None:
        user_profile = UserProfile.objects.get(id=event["user_profile_id"])
        Realm.objects.get(id=event["realm_id"])

        try:
            self._generate_and_send_recap(user_profile)
        except Exception:
            # If anything goes wrong (e.g. a transient network error talking
            # to the LLM provider), the user's client is left waiting
            # indefinitely for an event that will never arrive unless we
            # send *something* back. Log the real error for debugging, but
            # notify the client with a clear failure state rather than
            # letting the request hang forever.
            logger.exception("Failed to generate message recap for user %s", user_profile.id)
            failure_event = MessageRecapReadyEvent(
                recap_html=(
                    "<p>Sorry, something went wrong generating your recap. Please try again.</p>"
                ),
                conversations=[],
            )
            send_event_on_commit(user_profile.realm, failure_event, [user_profile.id])

    def _generate_and_send_recap(self, user_profile: UserProfile) -> None:
        conversations = get_unread_conversations_for_recap(user_profile)

        labeled_summaries: list[tuple[str, str]] = []
        conversation_refs: list[RecapConversationRef] = []

        def summarize_one(conversation: RecapConversation) -> tuple[RecapConversation, str]:
            # Each ThreadPoolExecutor thread gets its own lazy DB connection
            # that Django won't close automatically; close it explicitly
            # when this thread's work is done to avoid leaking connections.
            try:
                if len(conversation.unread_message_ids) < MIN_MESSAGES_FOR_SUMMARY:
                    message_ids = sorted(conversation.unread_message_ids)
                    user_message_flags: dict[int, list[str]] = {
                        message_id: [] for message_id in message_ids
                    }
                    messages = messages_for_ids(
                        message_ids=message_ids,
                        user_message_flags=user_message_flags,
                        search_fields={},
                        apply_markdown=False,
                        client_gravatar=True,
                        allow_empty_topic_name=True,
                        message_edit_history_visibility_policy=MessageEditHistoryVisibilityPolicyEnum.none.value,
                        user_profile=user_profile,
                        realm=user_profile.realm,
                    )
                    summary = " ".join(
                        f"{message['sender_full_name']}: {message['content']}"
                        for message in messages
                    )
                    return conversation, summary
                return conversation, _summarize_conversation(user_profile, conversation)
            finally:
                connections.close_all()

        with ThreadPoolExecutor(max_workers=MAX_CONCURRENT_SUMMARY_CALLS) as executor:
            results = list(executor.map(summarize_one, conversations))

        for conversation, summary in results:
            if conversation.conversation_type == "stream":
                label = f"#{conversation.stream_name} > {conversation.topic_name}"
            else:
                label = "Direct message"
            labeled_summaries.append((label, summary))

            representative_message_id = max(conversation.unread_message_ids)
            conversation_refs.append(
                RecapConversationRef(
                    conversation_type=conversation.conversation_type,
                    message_id=representative_message_id,
                    stream_id=conversation.stream_id,
                    stream_name=conversation.stream_name,
                    topic_name=conversation.topic_name,
                    other_user_id=conversation.other_user_id,
                    user_ids_string=conversation.user_ids_string,
                )
            )

        merged_markdown = _merge_summaries(labeled_summaries)
        recap_html = markdown_convert(
            merged_markdown, message_realm=user_profile.realm
        ).rendered_content

        event_to_send = MessageRecapReadyEvent(
            recap_html=recap_html,
            conversations=conversation_refs,
        )
        send_event_on_commit(user_profile.realm, event_to_send, [user_profile.id])
