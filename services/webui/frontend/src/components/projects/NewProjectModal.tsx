"use client";

/*
 * Adapted from references/donna/frontend/src/app/components/projects/NewProjectModal.tsx
 * (MIT (c) 2026 Donna Contributors, same owner). Reduced to a {name}-only create
 * dialog against the BFF's POST /projects. Donna's CM number, members/sharing,
 * standalone-document picker, and file pre-upload are all out of scope and
 * dropped (no auth, no folders, no standalone documents in the BFF).
 */

import { useState } from "react";
import { X } from "lucide-react";
import { createProject, type BffProject, type BffError } from "@/lib/bff";

interface Props {
    open: boolean;
    onClose: () => void;
    onCreated: (project: BffProject) => void;
}

export function NewProjectModal({ open, onClose, onCreated }: Props) {
    const [name, setName] = useState("");
    const [loading, setLoading] = useState(false);
    const [error, setError] = useState("");

    if (!open) return null;

    function reset() {
        setName("");
        setError("");
        setLoading(false);
    }

    function handleClose() {
        reset();
        onClose();
    }

    async function handleSubmit(event: React.FormEvent) {
        event.preventDefault();
        const trimmed = name.trim();
        if (!trimmed) return;
        setLoading(true);
        setError("");
        try {
            const project = await createProject(trimmed);
            onCreated(project);
            reset();
            onClose();
        } catch (err) {
            setError((err as BffError).detail || "Failed to create project");
            setLoading(false);
        }
    }

    return (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/20 p-4 backdrop-blur-xs">
            <div className="w-full max-w-md rounded-2xl bg-card text-card-foreground shadow-2xl">
                <div className="flex items-center justify-between px-6 pt-5 pb-2">
                    <div className="flex items-center gap-1.5 text-xs text-muted-foreground">
                        <span>Projects</span>
                        <span>›</span>
                        <span>New project</span>
                    </div>
                    <button
                        type="button"
                        onClick={handleClose}
                        aria-label="Close"
                        className="rounded-lg p-1.5 text-muted-foreground hover:bg-accent"
                    >
                        <X className="size-4" />
                    </button>
                </div>

                <form onSubmit={handleSubmit} className="px-6 pb-5">
                    <label htmlFor="project-name" className="sr-only">
                        Project name
                    </label>
                    <input
                        id="project-name"
                        type="text"
                        value={name}
                        onChange={(event) => setName(event.target.value)}
                        placeholder="Project name"
                        autoFocus
                        className="mt-2 w-full bg-transparent text-2xl tracking-tight text-foreground placeholder:text-muted-foreground/50 focus:outline-none"
                    />

                    {error && <p className="mt-3 text-sm text-destructive">{error}</p>}

                    <div className="mt-6 flex items-center justify-end gap-2">
                        <button
                            type="button"
                            onClick={handleClose}
                            className="rounded-lg px-4 py-2 text-sm text-muted-foreground hover:bg-accent"
                        >
                            Cancel
                        </button>
                        <button
                            type="submit"
                            disabled={!name.trim() || loading}
                            className="rounded-lg bg-primary px-5 py-2 text-sm font-medium text-primary-foreground hover:bg-primary/90 disabled:opacity-40"
                        >
                            {loading ? "Creating…" : "Create project"}
                        </button>
                    </div>
                </form>
            </div>
        </div>
    );
}
