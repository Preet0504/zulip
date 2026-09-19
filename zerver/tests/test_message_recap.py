from datetime import datetime, timezone
from unittest import mock

import time_machine
from openai.resources.chat.completions import Completions
from openai.types.chat import ChatCompletion
from typing_extensions import override

from zerver.actions.message_recap import RecapConversation
from zerver.lib.test_classes import ZulipTestCase
from zerver.lib.test_helpers import mock_queue_publish
from zerver.worker.message_recap import MessageRecapWorker, _conversation_cache_key


def fake_chat_completion(content: str) -> ChatCompletion:
    return ChatCompletion.model_validate(
        {
            "id": "chatcmpl-test",
            "created": 1738495155,
            "model": "openai/gpt-oss-120b",
            "object": "chat.completion",
            "choices": [
                {
                    "finish_reason": "stop",
                    "index": 0,
                    "message": {
                        "content": content,
                        "role": "assistant",
                        "tool_calls": None,
                        "function_call": None,
                    },
                }
            ],
            "usage": {
                "completion_tokens": 10,
                "prompt_tokens": 20,
                "total_tokens": 30,
            },
        }
    )


class MessageRecapViewTestCase(ZulipTestCase):
    @override
    def setUp(self) -> None:
        super().setUp()
        self.user = self.example_user("hamlet")
        self.login_user(self.user)
        not_last_day_of_any_month = datetime(2025, 2, 18, 1, tzinfo=timezone.utc)
        self.mocked_time_patcher = time_machine.travel(not_last_day_of_any_month, tick=False)
        self.mocked_time_patcher.start()
        self.addCleanup(self.mocked_time_patcher.stop)

    def test_enqueues_recap_job(self) -> None:
        with mock_queue_publish("zerver.views.message_recap.queue_event_on_commit") as m:
            result = self.client_post("/json/messages/recap")
        self.assert_json_success(result)
        m.assert_called_once()
        queue_name, event, _ = m.call_args[0]
        self.assertEqual(queue_name, "message_recap")
        self.assertEqual(event["user_profile_id"], self.user.id)
        self.assertEqual(event["realm_id"], self.user.realm_id)

    def test_cost_limit_gating(self) -> None:
        with self.settings(MAX_PER_USER_MONTHLY_AI_COST=0):
            result = self.client_post("/json/messages/recap")
        self.assert_json_error_contains(result, "Reached monthly limit for AI credits.")

    def test_ai_disabled(self) -> None:
        with self.settings(TOPIC_SUMMARIZATION_MODEL=None):
            result = self.client_post("/json/messages/recap")
        self.assert_json_error_contains(result, "AI features are not enabled")


class MessageRecapWorkerTestCase(ZulipTestCase):
    @override
    def setUp(self) -> None:
        super().setUp()
        self.user = self.example_user("hamlet")
        self.sender = self.example_user("cordelia")

    def _send_messages(self, topic_name: str, count: int) -> None:
        self.subscribe(self.user, "Verona")
        self.subscribe(self.sender, "Verona")
        for i in range(count):
            self.send_stream_message(
                self.sender, "Verona", content=f"Message {i}", topic_name=topic_name
            )

    def test_empty_unread_sends_caught_up_event(self) -> None:
        worker = MessageRecapWorker()
        with mock.patch("zerver.worker.message_recap.send_event_on_commit") as mock_send_event:
            worker.consume({"user_profile_id": self.user.id, "realm_id": self.user.realm_id})
        mock_send_event.assert_called_once()
        _, event, user_ids = mock_send_event.call_args[0]
        self.assertEqual(list(user_ids), [self.user.id])
        self.assertIn("caught up", event.recap_html)
        self.assertEqual(event.conversations, [])

    def test_below_threshold_skips_llm_call(self) -> None:
        self._send_messages("small topic", 2)
        worker = MessageRecapWorker()
        with (
            mock.patch.object(Completions, "create") as mock_create,
            mock.patch("zerver.worker.message_recap.send_event_on_commit") as mock_send_event,
        ):
            worker.consume({"user_profile_id": self.user.id, "realm_id": self.user.realm_id})
        mock_create.assert_not_called()
        mock_send_event.assert_called_once()
        _, event, _ = mock_send_event.call_args[0]
        self.assert_length(event.conversations, 1)
        self.assertIn("Message 0", event.recap_html)

    def test_summarizes_and_sends_event(self) -> None:
        self._send_messages("big topic", 5)
        worker = MessageRecapWorker()
        fake_response = fake_chat_completion("A concise summary of the conversation.")
        with (
            mock.patch.object(Completions, "create", return_value=fake_response) as mock_create,
            mock.patch("zerver.worker.message_recap.send_event_on_commit") as mock_send_event,
        ):
            worker.consume({"user_profile_id": self.user.id, "realm_id": self.user.realm_id})
        mock_create.assert_called_once()
        mock_send_event.assert_called_once()
        _, event, user_ids = mock_send_event.call_args[0]
        self.assertEqual(list(user_ids), [self.user.id])
        self.assert_length(event.conversations, 1)
        self.assertEqual(event.conversations[0].topic_name, "big topic")
        self.assertIn("A concise summary", event.recap_html)

    def test_llm_failure_sends_error_event_instead_of_hanging(self) -> None:
        # Regression test: a transient failure talking to the LLM provider
        # (e.g. a network error) must not leave the worker silently dying
        # with no event sent, which would leave the client waiting forever.
        self._send_messages("flaky topic", 5)
        worker = MessageRecapWorker()
        with (
            mock.patch.object(
                Completions, "create", side_effect=RuntimeError("simulated connection error")
            ),
            mock.patch("zerver.worker.message_recap.send_event_on_commit") as mock_send_event,
        ):
            worker.consume({"user_profile_id": self.user.id, "realm_id": self.user.realm_id})
        mock_send_event.assert_called_once()
        _, event, user_ids = mock_send_event.call_args[0]
        self.assertEqual(list(user_ids), [self.user.id])
        self.assertEqual(event.conversations, [])
        self.assertIn("went wrong", event.recap_html)

    def test_caching_avoids_duplicate_llm_call(self) -> None:
        self._send_messages("cached topic", 5)
        fake_response = fake_chat_completion("A concise summary of the conversation.")

        with mock.patch.object(Completions, "create", return_value=fake_response) as mock_create:
            worker = MessageRecapWorker()
            with mock.patch("zerver.worker.message_recap.send_event_on_commit"):
                worker.consume({"user_profile_id": self.user.id, "realm_id": self.user.realm_id})
            self.assertEqual(mock_create.call_count, 1)

            # Same unread messages, second recap request: should hit the cache.
            worker2 = MessageRecapWorker()
            with mock.patch("zerver.worker.message_recap.send_event_on_commit"):
                worker2.consume({"user_profile_id": self.user.id, "realm_id": self.user.realm_id})
            self.assertEqual(mock_create.call_count, 1)

    def test_multiple_conversations_are_merged(self) -> None:
        self._send_messages("topic one", 5)
        self._send_messages("topic two", 5)
        fake_response = fake_chat_completion("A summary.")
        worker = MessageRecapWorker()
        with (
            mock.patch.object(Completions, "create", return_value=fake_response) as mock_create,
            mock.patch("zerver.worker.message_recap.send_event_on_commit") as mock_send_event,
        ):
            worker.consume({"user_profile_id": self.user.id, "realm_id": self.user.realm_id})
        # One call per conversation (2), plus one merge call.
        self.assertEqual(mock_create.call_count, 3)
        _, event, _ = mock_send_event.call_args[0]
        self.assert_length(event.conversations, 2)


class RecapCacheKeyTestCase(ZulipTestCase):
    def test_cache_key_has_no_invalid_characters(self) -> None:
        conversation = RecapConversation(
            conversation_type="stream",
            unread_message_ids=[1, 2, 3],
            stream_id=1,
            stream_name="Some Channel",
            topic_name="a topic with spaces & symbols!",
        )
        key = _conversation_cache_key(conversation, [1, 2, 3])
        self.assertNotIn(" ", key)
        self.assertTrue(all(ord(c) < 128 for c in key))
