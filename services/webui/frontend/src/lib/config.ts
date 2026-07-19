/**
 * Env-pinned pipeline config reference (Q4). DEFAULT `legal-rag-default-1.8.0`
 * (nemo-all, buffered final, needs the guardrail pod). There is NO in-UI
 * picker: the ref is pinned by environment only. The provenance line always
 * renders the LIVE ref returned in the `final` envelope, so the three README
 * modes (1.8.0 / 1.4.0 / 1.2.0) are distinguishable on screen.
 *
 * The ref is read client-side so it can be included in the request body the
 * browser POSTs to the proxy (the proxy forwards that body verbatim). This is
 * a non-secret config identifier — NOT the orchestrator address, which stays
 * strictly server-side (`ORCHESTRATOR_URL`, never `NEXT_PUBLIC_`).
 */

export const DEFAULT_CONFIG_REF = "legal-rag-default-1.8.0";

export function getConfigRef(): string {
    const pinned = process.env.NEXT_PUBLIC_WEBUI_CONFIG_REF;
    return pinned && pinned.length > 0 ? pinned : DEFAULT_CONFIG_REF;
}

/**
 * Env-pinned GENERAL retrieval index name (project-scoped chat). DEFAULT
 * `legal-rag-bench` — the orchestrator's own default index. This is the index
 * added to the scope when the general-index toggle is ON; it is read from the
 * env pin so no literal is hardcoded in component logic (mirroring the
 * config-ref env pin above). Like the config ref it is a non-secret location
 * identifier that rides in the request body verbatim through the proxy.
 */
export const DEFAULT_GENERAL_INDEX = "legal-rag-bench";

export function getGeneralIndex(): string {
    const pinned = process.env.NEXT_PUBLIC_GENERAL_INDEX;
    return pinned && pinned.length > 0 ? pinned : DEFAULT_GENERAL_INDEX;
}

/**
 * Env-pinned UNGUARDED pipeline config ref — the config the guardrails toggle
 * selects when the user turns guards OFF. DEFAULT `legal-rag-default-1.2.0`
 * (no input/output guards; live token streaming). There is no released config
 * that is byte-identical to the guarded default minus guards, so OFF is a whole
 * different pinned config: the provenance line always renders the LIVE ref, so
 * which one actually ran is never hidden.
 */
export const DEFAULT_UNGUARDED_CONFIG_REF = "legal-rag-default-1.2.0";

export function getUnguardedConfigRef(): string {
    const pinned = process.env.NEXT_PUBLIC_WEBUI_UNGUARDED_CONFIG_REF;
    return pinned && pinned.length > 0 ? pinned : DEFAULT_UNGUARDED_CONFIG_REF;
}

/**
 * Pick the pipeline config ref for a turn from the guardrails toggle:
 *   ON  → the guarded pinned config (`getConfigRef`, default 1.8.0 nemo-all).
 *   OFF → the unguarded config (`getUnguardedConfigRef`, default 1.2.0).
 * Pure function of the toggle so the mapping is unit-testable without a DOM.
 */
export function resolveConfigRef(guardrailsOn: boolean): string {
    return guardrailsOn ? getConfigRef() : getUnguardedConfigRef();
}
