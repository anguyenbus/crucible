"use client";

import {
    createContext,
    useContext,
    useMemo,
    useState,
    type ReactNode,
} from "react";

/**
 * Top-level view switch for the two workspaces that share the Phase-1 sidebar
 * shell: "chat" (unchanged — Next proxy → orchestrator) and "projects" (the
 * Phase-2 BFF screens). Chat stays the default so the harvested experience is
 * untouched.
 */
export type WorkspaceView = "chat" | "projects";

interface WorkspaceViewValue {
    view: WorkspaceView;
    setView: (view: WorkspaceView) => void;
}

const WorkspaceViewContext = createContext<WorkspaceViewValue | undefined>(undefined);

export function WorkspaceViewProvider({ children }: { children: ReactNode }) {
    const [view, setView] = useState<WorkspaceView>("chat");
    const value = useMemo(() => ({ view, setView }), [view]);
    return (
        <WorkspaceViewContext.Provider value={value}>
            {children}
        </WorkspaceViewContext.Provider>
    );
}

export function useWorkspaceView(): WorkspaceViewValue {
    const context = useContext(WorkspaceViewContext);
    if (!context) {
        throw new Error(
            "useWorkspaceView must be used within a WorkspaceViewProvider",
        );
    }
    return context;
}
