"use strict";

const assert = require("node:assert/strict");

const {make_stream} = require("./lib/example_stream.cjs");
const {make_user} = require("./lib/example_user.cjs");
const {zrequire} = require("./lib/namespace.cjs");
const {run_test} = require("./lib/test.cjs");

const stream_data = zrequire("stream_data");
const people = zrequire("people");
const recap_ui = zrequire("recap_ui");

const verona = make_stream({stream_id: 3, name: "Verona"});
stream_data.add_sub_for_tests(verona);

const cordelia = make_user({
    user_id: 21,
    email: "cordelia@example.com",
    full_name: "Cordelia",
});
people.add_active_user(cordelia, "server_events");

run_test("conversation_url stream", () => {
    const url = recap_ui.conversation_url({
        conversation_type: "stream",
        message_id: 114,
        stream_id: 3,
        stream_name: "Verona",
        topic_name: "api design discussion",
    });
    assert.equal(url, "#narrow/channel/3-Verona/topic/api.20design.20discussion/near/114");
});

run_test("conversation_url pm", () => {
    const url = recap_ui.conversation_url({
        conversation_type: "pm",
        message_id: 50,
        other_user_id: cordelia.user_id,
    });
    assert.equal(url, "#narrow/dm/21-Cordelia/near/50");
});

run_test("conversation_url huddle", () => {
    const url = recap_ui.conversation_url({
        conversation_type: "huddle",
        message_id: 60,
        user_ids_string: "21,22",
    });
    assert.equal(url, "#narrow/dm/21,22-group/near/60");
});

run_test("conversation_url missing data returns undefined", () => {
    const url = recap_ui.conversation_url({
        conversation_type: "stream",
        message_id: 1,
    });
    assert.equal(url, undefined);
});
