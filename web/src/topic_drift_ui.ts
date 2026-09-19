import {$} from "jquery";

import render_topic_drift_suggestion_banner from "../templates/compose_banner/topic_drift_suggestion_banner.hbs";

import * as channel from "./channel.ts";
import * as compose_banner from "./compose_banner.ts";
import {$t} from "./i18n.ts";
import * as narrow_state from "./narrow_state.ts";

export type TopicDriftSuggestion = {
    message_id: number;
    stream_id: number;
    topic_name: string;
    suggested_topic_name: string;
};

const TOPIC_DRIFT_BANNER_CLASSNAME = "topic_drift_suggestion";

// Keyed by stream_id + topic_name, so a suggestion for a topic the user
// isn't currently viewing is still shown if they later navigate to it.
const pending_suggestions = new Map<string, TopicDriftSuggestion>();

export function suggestion_key(stream_id: number, topic_name: string): string {
    return `${stream_id}:${topic_name.toLowerCase()}`;
}

function current_suggestion(): TopicDriftSuggestion | undefined {
    const stream_id = narrow_state.stream_id();
    const topic_name = narrow_state.topic();
    if (stream_id === undefined || topic_name === undefined) {
        return undefined;
    }
    return pending_suggestions.get(suggestion_key(stream_id, topic_name));
}

function render(): void {
    const $container = $("#topic-drift-banner-container");
    const suggestion = current_suggestion();
    if (suggestion === undefined) {
        $container.empty();
        return;
    }
    const html = render_topic_drift_suggestion_banner({
        banner_type: compose_banner.INFO,
        classname: TOPIC_DRIFT_BANNER_CLASSNAME,
        button_text: $t({defaultMessage: "Rename topic"}),
        suggested_topic_name: suggestion.suggested_topic_name,
    });
    $container.html(html);
}

function clear_current_suggestion(): void {
    const stream_id = narrow_state.stream_id();
    const topic_name = narrow_state.topic();
    if (stream_id !== undefined && topic_name !== undefined) {
        pending_suggestions.delete(suggestion_key(stream_id, topic_name));
    }
    render();
}

export function handle_suggestion(suggestion: TopicDriftSuggestion): void {
    pending_suggestions.set(
        suggestion_key(suggestion.stream_id, suggestion.topic_name),
        suggestion,
    );
    render();
}

// Called on every hash change, so a suggestion that arrived while the
// user was elsewhere is shown when they navigate into that topic, and
// the banner is cleared when they navigate away from it.
export function update_for_current_narrow(): void {
    render();
}

export function initialize(): void {
    $("#topic-drift-banner-container").on("click", ".main-view-banner-action-button", (e) => {
        e.preventDefault();
        const suggestion = current_suggestion();
        if (suggestion === undefined) {
            return;
        }
        void channel.patch({
            url: "/json/messages/" + suggestion.message_id,
            data: {
                topic: suggestion.suggested_topic_name,
                propagate_mode: "change_all",
                send_notification_to_old_thread: false,
                send_notification_to_new_thread: false,
            },
            success() {
                clear_current_suggestion();
            },
        });
    });

    $("#topic-drift-banner-container").on("click", ".main-view-banner-close-button", (e) => {
        e.preventDefault();
        clear_current_suggestion();
    });
}
