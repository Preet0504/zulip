# Zulip — AI features fork

This is a fork of [Zulip](https://zulip.com), the open-source team chat
app, extended with two LLM-powered features built as a course
assignment:

- **Message Recap** — a "Recap" entry in the left sidebar that
  generates an AI summary of all your unread conversations, with
  links back to each one.
- **Topic Title Improver** — a background check that detects when an
  active conversation has drifted away from its topic title, and
  shows a banner suggesting a better one, with one-click rename or
  dismiss.

For engineering details on both features (design decisions, file
pointers, tradeoffs), see
[`implementation.md`](https://github.com/Preet0504/zulip/blob/main/implementation.md).

## Setup

The development environment is the standard Zulip one, documented in
full at
[`docs/development/setup-recommended.md`](https://github.com/Preet0504/zulip/blob/main/docs/development/setup-recommended.md).
On Windows, that means WSL 2; on macOS/Linux, Vagrant with Docker.
Condensed for the common case (from inside your Zulip checkout):

```console
$ ./tools/provision
$ source .venv/bin/activate
$ ./tools/run-dev
```

Then visit the URL printed in the terminal (typically
`http://localhost:9991/`) and log in with one of the seeded dev users
(e.g., `hamlet@zulip.com`, any password).

## Configuring the LLM API key

Both features call an OpenAI-compatible chat completion API — the dev
environment is configured for [Groq](https://groq.com), which has a
free tier.

1. Get an API key from the [Groq console](https://console.groq.com/keys).
2. Open `zproject/dev-secrets.conf` and add it under the `[secrets]`
   section:

   ```ini
   topic_summarization_api_key = <your key>
   ```

3. If `run-dev` is already running, restart it so the new setting is
   picked up.

`zproject/dev-secrets.conf` is gitignored and is never committed —
this is the only manual step required to get both AI features
working; everything else (which model, cost accounting, per-user
monthly budget) is preconfigured in `zproject/dev_settings.py`.

No new Python or JavaScript dependencies were added beyond what
upstream Zulip's topic-summarization feature already required
(`./tools/provision` installs everything).

## Running the tests

```console
$ ./tools/test-backend zerver.tests.test_message_recap zerver.tests.test_topic_drift
$ ./tools/test-js-with-node
```

## License

Distributed under the
[Apache 2.0](https://github.com/Preet0504/zulip/blob/main/LICENSE)
license, same as upstream Zulip.
