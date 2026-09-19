# Implementation notes

Demo video (both features): **[TODO: add link once recorded]**

## Feature 1 — Message Recap

An LLM-generated summary of every unread conversation a user has,
shown on a dedicated "Recap" page, with links back to each source
conversation.

**Backend.** `POST /json/messages/recap`
(`zerver/views/message_recap.py::generate_messages_recap`) gates on
`can_summarize_topics()` and the existing `MAX_PER_USER_MONTHLY_AI_COST`
budget, then enqueues a job on a new `message_recap` RabbitMQ queue via
`queue_event_on_commit` and returns immediately — no LLM work happens
on the request thread. `zerver/worker/message_recap.py::MessageRecapWorker`
consumes it: `zerver/actions/message_recap.py::get_unread_conversations_for_recap`
groups all unread messages into per-conversation buckets (channel+topic,
or a DM thread). This is a map-reduce over those buckets: each is
summarized independently with a bounded pool of concurrent OpenAI-compatible
calls against `settings.TOPIC_SUMMARIZATION_MODEL`
(`MAX_CONCURRENT_SUMMARY_CALLS = 5`, to respect provider rate limits),
skipping the LLM entirely for conversations below `MIN_MESSAGES_FOR_SUMMARY`
messages, then one final call merges the per-conversation summaries into
one recap, rendered through Zulip's Markdown pipeline. Per-conversation
summaries are cached in memcached (keyed on which message IDs were
included) so reopening the page before reading new messages doesn't
re-pay for the same call. On completion, `send_event_on_commit` pushes a
`message_recap_ready` event (`zerver/lib/event_types.py`) over the
user's existing Tornado long-poll connection — an LLM retry storm (we
hit one during development) never occupies a Django worker.

**Link generation.** The worker returns one `RecapConversationRef` per
conversation — `conversation_type`, `message_id`, and either
`stream_id`/`stream_name`/`topic_name` or `other_user_id`/
`user_ids_string` — inside the event payload. On the frontend,
`web/src/recap_ui.ts::conversation_url()` turns each ref into an actual
href: for a channel conversation, `hash_util.by_stream_topic_url(stream_id,
topic_name) + "/near/" + message_id`, which produces exactly
`#narrow/channel/<id>-<name>/topic/<topic>/near/<message_id>`; for a DM,
the equivalent `#narrow/dm/<slug>/near/<message_id>` via
`people.user_ids_string_to_slug`. `handle_recap_ready()` renders these as
a "Jump to a conversation" list below the recap text.

**Frontend integration.** A "Recap" entry is added to the left sidebar
(`navigation_views.ts`, `left_sidebar_navigation_area.ts`) at the `#recap`
hash. `recap_ui.ts::show()` immediately shows a loading state and fires
the POST; `server_events_dispatch.js` routes the resulting
`message_recap_ready` event to `handle_recap_ready`, which swaps in the
rendered recap and links. A client-side timeout shows a graceful error
if no event arrives (e.g. the queue worker is down), since the backend
can't always guarantee it can report its own failure.

## Feature 2 — Topic Title Improver

Detects when an active conversation has drifted from its topic's title
and suggests a better one, shortly after it happens.

**Backend & latency.** `zerver/actions/message_send.py::do_send_messages`
enqueues a job on a new `topic_drift` queue for every channel message,
right after it commits (same trigger point as the existing `embed_links`
worker) — detection starts within moments of the message landing, while
the user still has context. Like Feature 1, this is fully async:
`POST /json/messages` returns immediately, and
`zerver/worker/topic_drift.py::TopicDriftDetector` does the LLM call on a
background queue-worker process, so retries (the OpenAI client uses
`max_retries=5`) never contend with web traffic for Django's fixed worker
pool.

**Cost.** Only every 5th message in a topic
(`MESSAGES_BETWEEN_DRIFT_CHECKS`) triggers an actual LLM call — smaller
counts return immediately. A memcached atomic add-if-absent
(`_claim_drift_check`) deduplicates checks so a burst of messages landing
close together (e.g. several replies at once) still only pays for one
call, not one per message. Reuses the existing `can_summarize_topics()`
permission and `MAX_PER_USER_MONTHLY_AI_COST` budget, charged to the
message's sender.

**Scalability.** Because checks run on background workers, throughput
scales with the number of queue-worker processes, independent of
web-server capacity, and RabbitMQ buffers bursts instead of dropping or
blocking sends. No new per-topic state is persisted — the message-count
threshold and the memcached claim are both derived on the fly — so this
adds no extra write load to Postgres beyond ordinary message sends.

**Frontend integration.** A `topic_drift_suggestion` event
(`zerver/lib/event_types.py::TopicDriftSuggestionEvent`) is delivered the
same way as Feature 1. `web/src/topic_drift_ui.ts` keeps pending
suggestions in memory keyed by stream+topic, and renders a banner
(reusing Zulip's existing `compose_banner` component) only when the user
is actually viewing the matching topic. This hooks into
`message_lists.update_current_message_list()` rather than the hash-change
handler, since that's the one point guaranteed to fire for every narrow
change, including in-app sidebar clicks (a real bug we hit and fixed:
hashchange.ts skips its own handling for internally-triggered navigation).
"Rename topic" reuses the existing inline-topic-rename PATCH request;
dismiss only clears local state, per the assignment's explicit scope note
that persisting dismissal server-side isn't required.

**Known limitations.** Drift classification is one non-deterministic LLM
call per check with no disambiguation retry, so borderline cases can go
either way on repeated runs. The fixed 5-message cadence is a simple
heuristic, not adaptive to a topic's actual pace.
