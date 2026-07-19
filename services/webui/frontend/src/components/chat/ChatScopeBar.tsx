"use client";

import { useEffect, useState } from "react";
import { listProjects, type BffProject } from "@/lib/bff";
import { useChatScope } from "@/contexts/ChatScopeContext";

/**
 * Chat scope control (project-scoped chat, spec req 7): pick the project the
 * chat is grounded in and toggle the general legal index on top.
 *
 * - Project select drives the scope (`retrieval_indices = [proj-{id}]`).
 * - The general-index toggle DEFAULTS OFF (project-only); ON broadens to
 *   `[proj-{id}, <general>]`. It is disabled with no project selected (general
 *   chat already queries the general index by default).
 * - The frontend obtains index names from the BFF record / env pin only, never
 *   from the orchestrator.
 */
export function ChatScopeBar() {
    const { project, includeGeneral, guardrailsOn, setProject, setIncludeGeneral, setGuardrailsOn } =
        useChatScope();
    const [projects, setProjects] = useState<BffProject[]>([]);
    const [error, setError] = useState(false);

    useEffect(() => {
        let active = true;
        listProjects()
            .then((list) => {
                if (active) setProjects(list);
            })
            .catch(() => {
                if (active) setError(true);
            });
        return () => {
            active = false;
        };
    }, []);

    const onSelect = (id: string) => {
        if (!id) {
            setProject(null);
            return;
        }
        const found = projects.find((p) => p.id === id);
        if (found) {
            setProject({ id: found.id, name: found.name, indexName: found.index_name });
        }
    };

    return (
        <div className="mx-auto mb-2 flex w-full max-w-3xl flex-wrap items-center gap-3 px-4 text-xs text-muted-foreground">
            <label className="flex items-center gap-2">
                <span>Chat scope</span>
                <select
                    aria-label="Chat scope project"
                    value={project?.id ?? ""}
                    onChange={(e) => onSelect(e.target.value)}
                    className="rounded-md border border-border bg-card px-2 py-1 text-foreground outline-none"
                >
                    <option value="">General (no project)</option>
                    {projects.map((p) => (
                        <option key={p.id} value={p.id}>
                            {p.name}
                        </option>
                    ))}
                </select>
            </label>

            <label className="flex items-center gap-2">
                <input
                    type="checkbox"
                    checked={includeGeneral}
                    disabled={project === null}
                    onChange={(e) => setIncludeGeneral(e.target.checked)}
                    aria-label="Include the general legal index"
                    className="size-3.5 accent-current"
                />
                <span>Include general legal index</span>
            </label>

            <label className="flex items-center gap-2" title="ON = guarded config (1.8.0); OFF = no guards (1.2.0)">
                <input
                    type="checkbox"
                    checked={guardrailsOn}
                    onChange={(e) => setGuardrailsOn(e.target.checked)}
                    aria-label="Enable guardrails"
                    className="size-3.5 accent-current"
                />
                <span>Guardrails</span>
            </label>

            <span className="text-muted-foreground/80">
                {project === null
                    ? "Answers draw on the general legal corpus."
                    : includeGeneral
                      ? "Answers draw on this project’s documents and the general legal corpus."
                      : "Answers draw only on this project’s documents."}
            </span>
            {error && <span className="text-destructive">Could not load projects.</span>}
        </div>
    );
}
