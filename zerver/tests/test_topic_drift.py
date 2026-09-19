from unittest import mock

from openai.resources.chat.completions import Completions
from openai.types.chat import ChatCompletion

from zerver.lib.test_classes import ZulipTestCase
from zerver.lib.test_helpers import mock_queue_publish
from zerver.worker.topic_drift import (
    MESSAGES_BETWEEN_DRIFT_CHECKS,
    NO_DRIFT_SENTINEL,
    TopicDriftDetector,
)


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


class TopicDriftEnqueueTestCase(ZulipTestCase):
    def test_enqueues_for_channel_messages(self) -> None:
        hamlet = self.example_user("hamlet")
        with (
            self.settings(TEST_SUITE=False),
            mock_queue_publish("zerver.actions.message_send.queue_event_on_commit") as m,
        ):
            self.send_stream_message(hamlet, "Verona", topic_name="some topic")
        topic_drift_calls = [call for call in m.call_args_list if call.args[0] == "topic_drift"]
        self.assert_length(topic_drift_calls, 1)
        _, event, _ = topic_drift_calls[0].args
        self.assertEqual(event["topic_name"], "some topic")

    def test_does_not_enqueue_for_direct_messages(self) -> None:
        hamlet = self.example_user("hamlet")
        cordelia = self.example_user("cordelia")
        with (
            self.settings(TEST_SUITE=False),
            mock_queue_publish("zerver.actions.message_send.queue_event_on_commit") as m,
        ):
            self.send_personal_message(hamlet, cordelia)
        topic_drift_calls = [call for call in m.call_args_list if call.args[0] == "topic_drift"]
        self.assertEqual(topic_drift_calls, [])

    def test_skips_enqueue_during_test_suite(self) -> None:
        # TEST_SUITE is already True in the test environment; this worker
        # fires on every channel message (unlike embed_links, which only
        # fires when a message contains a link), so without this guard
        # ordinary tests elsewhere in the suite would make real LLM calls.
        hamlet = self.example_user("hamlet")
        with mock_queue_publish("zerver.actions.message_send.queue_event_on_commit") as m:
            self.send_stream_message(hamlet, "Verona")
        topic_drift_calls = [call for call in m.call_args_list if call.args[0] == "topic_drift"]
        self.assertEqual(topic_drift_calls, [])


class TopicDriftWorkerTestCase(ZulipTestCase):
    def _send_messages_and_get_event(self, topic_name: str, count: int) -> dict[str, int | str]:
        hamlet = self.example_user("hamlet")
        cordelia = self.example_user("cordelia")
        self.subscribe(hamlet, "Verona")
        self.subscribe(cordelia, "Verona")
        last_message_id = None
        for i in range(count):
            last_message_id = self.send_stream_message(
                cordelia, "Verona", content=f"Message {i}", topic_name=topic_name
            )
        assert last_message_id is not None
        stream_id = self.get_stream_id("Verona")
        return {
            "message_id": last_message_id,
            "stream_id": stream_id,
            "topic_name": topic_name,
        }

    def test_below_threshold_skips_llm_call(self) -> None:
        event = self._send_messages_and_get_event("quiet topic", MESSAGES_BETWEEN_DRIFT_CHECKS - 1)
        worker = TopicDriftDetector()
        with (
            mock.patch.object(Completions, "create") as mock_create,
            mock.patch("zerver.worker.topic_drift.send_event_on_commit") as mock_send_event,
        ):
            worker.consume(event)
        mock_create.assert_not_called()
        mock_send_event.assert_not_called()

    def test_no_drift_sends_no_event(self) -> None:
        event = self._send_messages_and_get_event("steady topic", MESSAGES_BETWEEN_DRIFT_CHECKS)
        worker = TopicDriftDetector()
        fake_response = fake_chat_completion(NO_DRIFT_SENTINEL)
        with (
            mock.patch.object(Completions, "create", return_value=fake_response) as mock_create,
            mock.patch("zerver.worker.topic_drift.send_event_on_commit") as mock_send_event,
        ):
            worker.consume(event)
        mock_create.assert_called_once()
        mock_send_event.assert_not_called()

    def test_drift_sends_suggestion_event(self) -> None:
        event = self._send_messages_and_get_event("old title", MESSAGES_BETWEEN_DRIFT_CHECKS)
        worker = TopicDriftDetector()
        fake_response = fake_chat_completion("A much better title")
        with (
            mock.patch.object(Completions, "create", return_value=fake_response),
            mock.patch("zerver.worker.topic_drift.send_event_on_commit") as mock_send_event,
        ):
            worker.consume(event)
        mock_send_event.assert_called_once()
        _, sent_event, user_ids = mock_send_event.call_args[0]
        self.assertEqual(sent_event.suggested_topic_name, "A much better title")
        self.assertEqual(sent_event.topic_name, "old title")
        self.assertEqual(sent_event.stream_id, event["stream_id"])
        self.assertIn(self.example_user("cordelia").id, list(user_ids))

    def test_duplicate_job_at_same_boundary_only_calls_llm_once(self) -> None:
        # Simulates several messages landing in the topic close together
        # (e.g. a bulk import), whose jobs could otherwise all observe the
        # same MESSAGES_BETWEEN_DRIFT_CHECKS boundary and all pay for
        # their own LLM call.
        event = self._send_messages_and_get_event("busy topic", MESSAGES_BETWEEN_DRIFT_CHECKS)
        fake_response = fake_chat_completion(NO_DRIFT_SENTINEL)
        with mock.patch.object(Completions, "create", return_value=fake_response) as mock_create:
            TopicDriftDetector().consume(event)
            TopicDriftDetector().consume(event)
        mock_create.assert_called_once()

    def test_cost_limit_gating_skips_llm_call(self) -> None:
        event = self._send_messages_and_get_event("expensive topic", MESSAGES_BETWEEN_DRIFT_CHECKS)
        worker = TopicDriftDetector()
        with (
            self.settings(MAX_PER_USER_MONTHLY_AI_COST=0),
            mock.patch.object(Completions, "create") as mock_create,
        ):
            worker.consume(event)
        mock_create.assert_not_called()

    def test_ai_disabled_skips_llm_call(self) -> None:
        event = self._send_messages_and_get_event("disabled topic", MESSAGES_BETWEEN_DRIFT_CHECKS)
        worker = TopicDriftDetector()
        with (
            self.settings(TOPIC_SUMMARIZATION_MODEL=None),
            mock.patch.object(Completions, "create") as mock_create,
        ):
            worker.consume(event)
        mock_create.assert_not_called()

    def test_moved_topic_before_consume_skips(self) -> None:
        # Simulates the message having moved to a different topic between
        # when the job was enqueued and when the worker picks it up, by
        # giving consume() an event whose recorded topic_name no longer
        # matches the message's actual (unmodified) topic.
        event = self._send_messages_and_get_event("original name", MESSAGES_BETWEEN_DRIFT_CHECKS)
        event = {**event, "topic_name": "a different topic entirely"}

        worker = TopicDriftDetector()
        with (
            mock.patch.object(Completions, "create") as mock_create,
            mock.patch("zerver.worker.topic_drift.send_event_on_commit") as mock_send_event,
        ):
            worker.consume(event)
        mock_create.assert_not_called()
        mock_send_event.assert_not_called()
