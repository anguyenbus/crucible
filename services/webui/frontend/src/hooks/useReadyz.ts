"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { probeReadyz, type ReadyzState } from "@/lib/readyz";

const AUTO_RETRY_MS = 10_000;

/**
 * Drive the readyz banner gate: probe at load, auto-retry on an interval while
 * not ready, and expose a manual retry. Recovery re-enables input without a
 * page reload (not a hard-stop).
 */
export function useReadyz() {
    const [state, setState] = useState<ReadyzState>({ ready: true, errorText: null });
    const [checking, setChecking] = useState(true);
    const timerRef = useRef<ReturnType<typeof setTimeout> | null>(null);

    const probe = useCallback(async () => {
        setChecking(true);
        const next = await probeReadyz();
        setState(next);
        setChecking(false);
        return next;
    }, []);

    useEffect(() => {
        let cancelled = false;
        const run = async () => {
            const next = await probe();
            if (cancelled) return;
            if (!next.ready) {
                timerRef.current = setTimeout(run, AUTO_RETRY_MS);
            }
        };
        void run();
        return () => {
            cancelled = true;
            if (timerRef.current) clearTimeout(timerRef.current);
        };
    }, [probe]);

    const retry = useCallback(() => {
        if (timerRef.current) clearTimeout(timerRef.current);
        void probe();
    }, [probe]);

    return { ...state, checking, retry };
}
