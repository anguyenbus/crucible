"use client";

/*
 * Adapted from references/donna/frontend/src/app/components/projects/ProjectsOverview.tsx
 * (MIT (c) 2026 Donna Contributors, same owner). The projects table: name,
 * document count, created date, create/rename/delete, drilling into a project's
 * documents. Donna's owner/shared tabs, checkbox bulk-owner gating, is_owner/
 * user_id gating, CM-number and Tabular-Reviews columns, folders, and useAuth
 * are all out of scope and dropped — the BFF has no auth and no such surface.
 */

import { useCallback, useEffect, useState } from "react";
import { FolderOpen, Pencil, Plus, Trash2 } from "lucide-react";
import {
    deleteProject,
    listProjects,
    renameProject,
    type BffProject,
} from "@/lib/bff";
import { NewProjectModal } from "./NewProjectModal";
import { ProjectDetailPanel } from "./ProjectDetailPanel";

function formatDate(iso: string): string {
    const date = new Date(iso);
    if (Number.isNaN(date.getTime())) return "—";
    return date.toLocaleDateString(undefined, {
        day: "numeric",
        month: "short",
        year: "numeric",
    });
}

export function ProjectsWorkspace() {
    const [projects, setProjects] = useState<BffProject[]>([]);
    const [loading, setLoading] = useState(true);
    const [error, setError] = useState<string | null>(null);
    const [modalOpen, setModalOpen] = useState(false);
    const [renamingId, setRenamingId] = useState<string | null>(null);
    const [renameValue, setRenameValue] = useState("");
    const [selectedProject, setSelectedProject] = useState<BffProject | null>(null);

    async function load() {
        setLoading(true);
        setError(null);
        try {
            setProjects(await listProjects());
        } catch {
            setError("Could not reach the BFF to list projects.");
        } finally {
            setLoading(false);
        }
    }

    useEffect(() => {
        void load();
    }, []);

    async function handleRenameSubmit(projectId: string) {
        const trimmed = renameValue.trim();
        setRenamingId(null);
        if (!trimmed) return;
        setProjects((prev) =>
            prev.map((p) => (p.id === projectId ? { ...p, name: trimmed } : p)),
        );
        await renameProject(projectId, trimmed);
    }

    async function handleDelete(projectId: string) {
        await deleteProject(projectId);
        setProjects((prev) => prev.filter((p) => p.id !== projectId));
    }

    // Stable identity: ProjectDetailPanel's load() depends on this callback and
    // calls it after fetching. An unmemoized function here would change every
    // render, recreating the child's load() and refiring its effect in an
    // infinite fetch loop (the "Loading documents…" flashing). setProjects is
    // stable, so the empty dep list is correct.
    const handleDocumentCountChange = useCallback((projectId: string, count: number) => {
        setProjects((prev) =>
            prev.map((p) => (p.id === projectId ? { ...p, document_count: count } : p)),
        );
    }, []);

    if (selectedProject) {
        return (
            <ProjectDetailPanel
                projectId={selectedProject.id}
                projectName={selectedProject.name}
                onBack={() => {
                    setSelectedProject(null);
                    void load();
                }}
                onDocumentCountChange={handleDocumentCountChange}
            />
        );
    }

    return (
        <div className="flex-1 overflow-y-auto">
            <div className="flex items-center justify-between px-8 py-4">
                <h1 className="text-2xl font-medium tracking-tight text-foreground">
                    Projects
                </h1>
                <button
                    type="button"
                    onClick={() => setModalOpen(true)}
                    className="inline-flex items-center gap-1.5 rounded-lg bg-primary px-3 py-1.5 text-sm font-medium text-primary-foreground hover:bg-primary/90"
                >
                    <Plus className="size-4" />
                    New project
                </button>
            </div>

            <div className="px-8 pb-8">
                <div className="flex h-9 items-center border-b border-border text-xs font-medium text-muted-foreground">
                    <div className="flex-1">Name</div>
                    <div className="w-28 shrink-0">Documents</div>
                    <div className="w-32 shrink-0">Created</div>
                    <div className="w-16 shrink-0" />
                </div>

                {loading ? (
                    <p className="py-16 text-center text-sm text-muted-foreground">
                        Loading projects…
                    </p>
                ) : error ? (
                    <p className="py-16 text-center text-sm text-destructive">{error}</p>
                ) : projects.length === 0 ? (
                    <div className="flex flex-col items-center py-24 text-center">
                        <FolderOpen className="mb-4 size-8 text-muted-foreground/50" />
                        <p className="text-sm text-muted-foreground">
                            No projects yet.
                        </p>
                        <button
                            type="button"
                            onClick={() => setModalOpen(true)}
                            className="mt-4 inline-flex items-center gap-1 rounded-full bg-primary px-3 py-1 text-xs font-medium text-primary-foreground hover:bg-primary/90"
                        >
                            <Plus className="size-3" />
                            Create your first project
                        </button>
                    </div>
                ) : (
                    <ul>
                        {projects.map((project) => (
                            <li
                                key={project.id}
                                className="group flex h-12 items-center border-b border-border/60 text-sm hover:bg-accent/50"
                            >
                                <div className="flex-1 min-w-0 pr-4">
                                    {renamingId === project.id ? (
                                        <input
                                            autoFocus
                                            value={renameValue}
                                            onChange={(e) => setRenameValue(e.target.value)}
                                            onKeyDown={(e) => {
                                                if (e.key === "Enter")
                                                    void handleRenameSubmit(project.id);
                                                if (e.key === "Escape") setRenamingId(null);
                                            }}
                                            onBlur={() => void handleRenameSubmit(project.id)}
                                            className="w-full bg-transparent text-foreground focus:outline-none"
                                        />
                                    ) : (
                                        <button
                                            type="button"
                                            onClick={() => setSelectedProject(project)}
                                            className="block max-w-full truncate text-left text-foreground hover:underline"
                                        >
                                            {project.name}
                                        </button>
                                    )}
                                </div>
                                <div className="w-28 shrink-0 text-muted-foreground">
                                    {project.document_count}
                                </div>
                                <div className="w-32 shrink-0 text-muted-foreground">
                                    {formatDate(project.created_at)}
                                </div>
                                <div className="flex w-16 shrink-0 items-center justify-end gap-1 opacity-0 group-hover:opacity-100">
                                    <button
                                        type="button"
                                        aria-label={`Rename ${project.name}`}
                                        onClick={() => {
                                            setRenameValue(project.name);
                                            setRenamingId(project.id);
                                        }}
                                        className="rounded-md p-1.5 text-muted-foreground hover:bg-accent"
                                    >
                                        <Pencil className="size-3.5" />
                                    </button>
                                    <button
                                        type="button"
                                        aria-label={`Delete ${project.name}`}
                                        onClick={() => void handleDelete(project.id)}
                                        className="rounded-md p-1.5 text-muted-foreground hover:bg-accent hover:text-destructive"
                                    >
                                        <Trash2 className="size-3.5" />
                                    </button>
                                </div>
                            </li>
                        ))}
                    </ul>
                )}
            </div>

            <NewProjectModal
                open={modalOpen}
                onClose={() => setModalOpen(false)}
                onCreated={(project) => setProjects((prev) => [project, ...prev])}
            />
        </div>
    );
}
