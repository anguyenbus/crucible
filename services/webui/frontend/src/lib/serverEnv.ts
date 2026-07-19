/**
 * Server-side-ONLY environment access for the route-handler proxy.
 *
 * `ORCHESTRATOR_URL` is read here and NOWHERE else, and is NEVER exposed to the
 * browser: it must never become a `NEXT_PUBLIC_` variable (that would leak the
 * orchestrator address into the client bundle and break the dependency
 * firewall). No compose service-name assumptions are baked in — the default is
 * localhost, and the pod future overrides it purely by env.
 *
 * This module must only be imported from server code (route handlers).
 */

export function orchestratorUrl(): string {
    const url = process.env.ORCHESTRATOR_URL;
    return url && url.length > 0 ? url.replace(/\/+$/, "") : "http://localhost:8000";
}
