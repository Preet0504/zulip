from dataclasses import dataclass
from typing import Literal

from zerver.lib.message import aggregate_unread_data, get_raw_unread_data
from zerver.models import UserProfile
from zerver.models.streams import Stream


@dataclass
class RecapConversation:
    conversation_type: Literal["stream", "pm", "huddle"]
    unread_message_ids: list[int]
    stream_id: int | None = None
    stream_name: str | None = None
    topic_name: str | None = None
    # For "pm": the other participant. For "huddle": comma-separated user IDs.
    other_user_id: int | None = None
    user_ids_string: str | None = None


def get_unread_conversations_for_recap(user_profile: UserProfile) -> list[RecapConversation]:
    raw_data = get_raw_unread_data(user_profile)
    unread_data = aggregate_unread_data(raw_data, allow_empty_topic_name=True)

    stream_ids = {stream_info["stream_id"] for stream_info in unread_data["streams"]}
    stream_names = dict(Stream.objects.filter(id__in=stream_ids).values_list("id", "name"))

    conversations: list[RecapConversation] = []

    for stream_info in unread_data["streams"]:
        conversations.append(
            RecapConversation(
                conversation_type="stream",
                unread_message_ids=stream_info["unread_message_ids"],
                stream_id=stream_info["stream_id"],
                stream_name=stream_names.get(stream_info["stream_id"], ""),
                topic_name=stream_info["topic"],
            )
        )

    for pm_info in unread_data["pms"]:
        conversations.append(
            RecapConversation(
                conversation_type="pm",
                unread_message_ids=pm_info["unread_message_ids"],
                other_user_id=pm_info["other_user_id"],
            )
        )

    for huddle_info in unread_data["huddles"]:
        conversations.append(
            RecapConversation(
                conversation_type="huddle",
                unread_message_ids=huddle_info["unread_message_ids"],
                user_ids_string=huddle_info["user_ids_string"],
            )
        )

    return conversations
