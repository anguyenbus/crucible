/**
 * `text/event-stream` framing parser — the TS mirror of the orchestrator's
 * Chainlit `demo_ui/chat_ui/sse.py`.
 *
 * The orchestrator's SSE contract is three typed events: `token`* then EXACTLY
 * ONE `final` | `error`, each a single JSON `data:` payload. This module is
 * deliberately dependency-free and framework-free so it is unit-tested without
 * a browser (the `demo_ui` browser-free-logic pattern). It parses incrementally
 * so a chunked byte stream whose boundaries fall mid-line still frames cleanly.
 */

import type { QueryResponseEnvelope, StreamError, StreamEvent } from "./envelope";

/** One parsed SSE frame: the event name plus its decoded JSON data. */
export interface SSEEvent {
    event: string;
    data: unknown;
}

/**
 * Incremental line-level SSE parser: `feed` each line, get a frame on the blank
 * line that dispatches it. Multiple `data:` lines are joined with newlines
 * before JSON decoding; comment lines (leading `:`) are ignored; a frame with
 * no data is dropped.
 */
export class SSELineParser {
    private event = "";
    private dataLines: string[] = [];

    feed(line: string): SSEEvent | null {
        if (line === "") return this.dispatch();
        if (line.startsWith(":")) return null; // SSE comment / keep-alive
        const colon = line.indexOf(":");
        const field = colon === -1 ? line : line.slice(0, colon);
        let value = colon === -1 ? "" : line.slice(colon + 1);
        if (value.startsWith(" ")) value = value.slice(1);
        if (field === "event") {
            this.event = value;
        } else if (field === "data") {
            this.dataLines.push(value);
        }
        // Other SSE fields (id, retry) are not part of the contract — ignored.
        return null;
    }

    private dispatch(): SSEEvent | null {
        const eventName = this.event;
        const dataLines = this.dataLines;
        this.event = "";
        this.dataLines = [];
        if (dataLines.length === 0) return null;
        return {
            event: eventName || "message",
            data: JSON.parse(dataLines.join("\n")),
        };
    }
}

/** Parse a complete list of stream lines (test/offline convenience). */
export function parseSSELines(lines: string[]): SSEEvent[] {
    const parser = new SSELineParser();
    const events: SSEEvent[] = [];
    for (const line of lines) {
        const event = parser.feed(line);
        if (event !== null) events.push(event);
    }
    // A final frame not terminated by a blank line still dispatches.
    const trailing = parser.feed("");
    if (trailing !== null) events.push(trailing);
    return events;
}

/**
 * Byte-stream framing over incremental chunks. Buffers a partial trailing line
 * across `push` calls so chunk boundaries mid-line are handled, then dispatches
 * completed frames. `flush` drains any trailing frame at end-of-stream.
 */
export class SSEByteStreamParser {
    private readonly decoder = new TextDecoder();
    private readonly lineParser = new SSELineParser();
    private buffer = "";

    push(chunk: Uint8Array): SSEEvent[] {
        this.buffer += this.decoder.decode(chunk, { stream: true });
        return this.drainLines(false);
    }

    flush(): SSEEvent[] {
        this.buffer += this.decoder.decode();
        const events = this.drainLines(true);
        // Dispatch a final unterminated frame (matches parseSSELines).
        const trailing = this.lineParser.feed("");
        if (trailing !== null) events.push(trailing);
        return events;
    }

    private drainLines(includeLast: boolean): SSEEvent[] {
        const events: SSEEvent[] = [];
        const parts = this.buffer.split("\n");
        this.buffer = includeLast ? "" : (parts.pop() ?? "");
        for (const raw of parts) {
            // Tolerate CRLF line endings.
            const line = raw.endsWith("\r") ? raw.slice(0, -1) : raw;
            const event = this.lineParser.feed(line);
            if (event !== null) events.push(event);
        }
        return events;
    }
}

/** Parse a full list of byte chunks to raw frames (test convenience). */
export function parseSSEByteChunks(chunks: Uint8Array[]): SSEEvent[] {
    const parser = new SSEByteStreamParser();
    const events: SSEEvent[] = [];
    for (const chunk of chunks) events.push(...parser.push(chunk));
    events.push(...parser.flush());
    return events;
}

/**
 * Classify a raw SSE frame into the typed rendered-subset union. Unknown event
 * names (not part of the contract) return null and are ignored by callers.
 */
export function classifyEvent(frame: SSEEvent): StreamEvent | null {
    switch (frame.event) {
        case "token": {
            const data = frame.data as { text?: unknown };
            return { type: "token", text: String(data.text ?? "") };
        }
        case "final":
            return { type: "final", envelope: frame.data as QueryResponseEnvelope };
        case "error":
            return { type: "error", error: frame.data as StreamError };
        default:
            return null;
    }
}

/** Convenience: parse byte chunks straight to typed stream events. */
export function typedEventsFromByteChunks(chunks: Uint8Array[]): StreamEvent[] {
    const typed: StreamEvent[] = [];
    for (const frame of parseSSEByteChunks(chunks)) {
        const event = classifyEvent(frame);
        if (event !== null) typed.push(event);
    }
    return typed;
}

/** Encode a UTF-8 byte chunk (test helper: build scripted byte streams). */
export function encodeChunk(text: string): Uint8Array {
    return new TextEncoder().encode(text);
}
