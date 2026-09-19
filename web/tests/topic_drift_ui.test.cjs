"use strict";

const assert = require("node:assert/strict");

const {zrequire} = require("./lib/namespace.cjs");
const {run_test} = require("./lib/test.cjs");

const topic_drift_ui = zrequire("topic_drift_ui");

run_test("suggestion_key is case-insensitive on topic name", () => {
    assert.equal(
        topic_drift_ui.suggestion_key(3, "Api Design"),
        topic_drift_ui.suggestion_key(3, "api design"),
    );
});

run_test("suggestion_key differs by stream_id", () => {
    assert.notEqual(
        topic_drift_ui.suggestion_key(3, "api design"),
        topic_drift_ui.suggestion_key(4, "api design"),
    );
});

run_test("suggestion_key differs by topic name", () => {
    assert.notEqual(
        topic_drift_ui.suggestion_key(3, "api design"),
        topic_drift_ui.suggestion_key(3, "lunch plans"),
    );
});
