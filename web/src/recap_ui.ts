import {$} from "jquery";

import * as channel from "./channel.ts";
import * as hash_util from "./hash_util.ts";
import {$t} from "./i18n.ts";
import * as inbox_ui from "./inbox_ui.ts";
import * as internal_url from "./internal_url.ts";
import * as left_sidebar_navigation_area from "./left_sidebar_navigation_area.ts";
import * as narrow_title from "./narrow_title.ts";
import * as people from "./people.ts";
import * as recap_util from "./recap_util.ts";
import * as recent_view_ui from "./recent_view_ui.ts";
import * as rendered_markdown from "./rendered_markdown.ts";

export type RecapConversationRef = {
    conversation_type: "stream" | "pm" | "huddle";
    message_id: number;
    stream_id?: number;
    stream_name?: string;
    topic_name?: string;
    other_user_id?: number;
    user_ids_string?: string;
};

export function conversation_url(conversation: RecapConversationRef): string | undefined {
    const suffix = "/near/" + internal_url.encodeHashComponent(conversation.message_id.toString());
    if (
        conversation.conversation_type === "stream" &&
        conversation.stream_id !== undefined &&
        conversation.topic_name !== undefined
    ) {
        return hash_util.by_stream_topic_url(conversation.stream_id, conversation.topic_name) + suffix;
    }
    if (conversation.conversation_type === "pm" && conversation.other_user_id !== undefined) {
        const slug = people.user_ids_string_to_slug(conversation.other_user_id.toString());
        if (slug === undefined) {
            return undefined;
        }
        return "#narrow/dm/" + slug + suffix;
    }
    if (conversation.conversation_type === "huddle" && conversation.user_ids_string !== undefined) {
        return hash_util.direct_message_group_with_url(conversation.user_ids_string) + suffix;
    }
    return undefined;
}

// Safety-net timeout: the backend now reports failures back to the client
// as an event (rather than just dying silently), but this covers cases
// where even that can't happen (e.g. the queue worker itself is down).
// Chosen to comfortably exceed the backend's own retry budget.
const RECAP_TIMEOUT_MS = 60000;
let recap_timeout_id: ReturnType<typeof setTimeout> | undefined;

function clear_recap_timeout(): void {
    if (recap_timeout_id !== undefined) {
        clearTimeout(recap_timeout_id);
        recap_timeout_id = undefined;
    }
}

function render_loading(): void {
    $("#recap-pane").html(
        `<div class="recap-loading"><p>${$t({defaultMessage: "Generating your recap…"})}</p></div>`,
    );
}

function render_error(): void {
    $("#recap-pane").html(
        `<div class="recap-error"><p>${$t({
            defaultMessage: "Something went wrong generating your recap. Please try again.",
        })}</p></div>`,
    );
}

export function handle_recap_ready(recap_html: string, conversations: RecapConversationRef[]): void {
    clear_recap_timeout();
    if (!recap_util.is_visible()) {
        // The user navigated away before the recap finished; nothing to update.
        return;
    }

    const $links = $("<ul>").addClass("recap-conversation-links");
    for (const conversation of conversations) {
        const url = conversation_url(conversation);
        if (url === undefined) {
            continue;
        }
        const label =
            conversation.conversation_type === "stream"
                ? `#${conversation.stream_name} > ${conversation.topic_name}`
                : $t({defaultMessage: "Direct message"});
        $links.append($("<li>").append($("<a>").attr("href", url).text(label)));
    }

    const $content = $("<div>").addClass("recap-content rendered_markdown").html(recap_html);
    rendered_markdown.update_elements($content);

    $("#recap-pane").empty().append($content);
    if (conversations.length > 0) {
        $("#recap-pane").append($("<div>").addClass("recap-links-header").text($t({defaultMessage: "Jump to a conversation:"})));
        $("#recap-pane").append($links);
    }
}

function trigger_recap_generation(): void {
    render_loading();
    clear_recap_timeout();
    recap_timeout_id = setTimeout(() => {
        if (recap_util.is_visible()) {
            render_error();
        }
    }, RECAP_TIMEOUT_MS);
    void channel.post({
        url: "/json/messages/recap",
        error() {
            clear_recap_timeout();
            render_error();
        },
    });
}

export function show(): void {
    if (recap_util.is_visible()) {
        return;
    }
    // inbox_ui.hide()/recent_view_ui.hide() each unconditionally re-show
    // #message_feed_container as a side effect (views_util.hide does this
    // regardless of whether that view was actually visible), so call our
    // own hide() for it last to make sure it's the one that sticks.
    inbox_ui.hide();
    recent_view_ui.hide();
    $("#message_feed_container").hide();
    $("#recap-view").show();
    recap_util.set_visible(true);
    left_sidebar_navigation_area.highlight_recap_view();
    narrow_title.update_narrow_title();
    trigger_recap_generation();
}

export function hide(): void {
    if (!recap_util.is_visible()) {
        return;
    }
    clear_recap_timeout();
    $("#recap-view").hide();
    recap_util.set_visible(false);
    $("#message_feed_container").show();
}

export function initialize(): void {
    // No additional event handlers are needed beyond the hash-based
    // navigation wired up in hashchange.ts.
}
