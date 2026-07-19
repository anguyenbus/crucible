"use client";

import ReactMarkdown, { type Components, type Options } from "react-markdown";
import remarkGfm from "remark-gfm";
import { segmentCitations, sourceCardId } from "@/lib/citations";

/**
 * Render the (already-numbered) answer as markdown via react-markdown +
 * remark-gfm — NEVER innerHTML. Valid inline `[n]` citation markers become
 * clickable anchors that scroll to their numbered source card; any `[n]` that
 * is not a real citation is left as plain text (honest degradation).
 */

interface MdNode {
    type: string;
    value?: string;
    url?: string;
    children?: MdNode[];
}

/**
 * remark attacher: split text nodes on valid `[n]` markers into link nodes
 * pointing at `#source-card-<n>`. Runs on the parsed mdast, so a `[n]` that
 * commonmark already left as literal text (no matching link definition) is what
 * we rewrite — we never fabricate a link for an unknown marker.
 */
function remarkCitations(validNumbers: Set<number>) {
    return (tree: MdNode) => {
        const visit = (node: MdNode) => {
            if (!node.children) return;
            const next: MdNode[] = [];
            for (const child of node.children) {
                if (child.type === "text" && typeof child.value === "string") {
                    const segments = segmentCitations(child.value, validNumbers);
                    if (!segments.some((s) => s.type === "cite")) {
                        next.push(child);
                        continue;
                    }
                    for (const segment of segments) {
                        if (segment.type === "text") {
                            next.push({ type: "text", value: segment.value });
                        } else {
                            next.push({
                                type: "link",
                                url: `#${sourceCardId(segment.number)}`,
                                children: [{ type: "text", value: segment.anchor }],
                            });
                        }
                    }
                } else {
                    visit(child);
                    next.push(child);
                }
            }
            node.children = next;
        };
        visit(tree);
    };
}

function scrollToSourceCard(id: string) {
    const el = document.getElementById(id);
    if (!el) return;
    // Expand the collapsible "Cited sources" section if the card is inside a
    // collapsed <details>, so click-to-scroll still reveals it.
    (el.closest("details") as HTMLDetailsElement | null)?.setAttribute("open", "");
    el.scrollIntoView({ behavior: "smooth", block: "center" });
    el.classList.remove("source-card-flash");
    void el.offsetWidth; // reflow so the flash re-triggers on repeated clicks
    el.classList.add("source-card-flash");
}

export function AnswerMarkdown({
    text,
    validNumbers,
}: {
    text: string;
    validNumbers: Set<number>;
}) {
    const remarkPlugins = [
        remarkGfm,
        [remarkCitations, validNumbers],
    ] as unknown as Options["remarkPlugins"];

    const components: Components = {
        a({ href, children, ...props }) {
            if (href && href.startsWith("#source-card-")) {
                const id = href.slice(1);
                return (
                    <button
                        type="button"
                        className="mx-0.5 rounded bg-blue-50 px-1 align-baseline text-[0.85em] font-medium text-[rgb(0,136,255)] hover:underline"
                        onClick={() => scrollToSourceCard(id)}
                    >
                        {children}
                    </button>
                );
            }
            return (
                <a href={href} {...props}>
                    {children}
                </a>
            );
        },
    };

    return (
        <div className="answer-prose text-sm leading-relaxed text-foreground">
            <ReactMarkdown remarkPlugins={remarkPlugins} components={components}>
                {text}
            </ReactMarkdown>
        </div>
    );
}
