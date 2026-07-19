import { ShieldAlert } from "lucide-react";
import type { GuardrailDecision } from "@/lib/envelope";
import { guardrailChip } from "@/lib/render";

/**
 * Minimal guardrail chip: action + rule count. Renders ONLY when
 * `guardrail_decisions` is non-empty (including the input-guard refusal `final`,
 * which carries zero tokens). No rationale panels, no per-rule detail, no
 * Phoenix trace links — those are Phase 3.
 */
export function GuardrailChip({ decisions }: { decisions: GuardrailDecision[] }) {
    const chip = guardrailChip(decisions);
    if (chip === null) return null;
    return (
        <span
            className="inline-flex items-center gap-1.5 rounded-full border border-border bg-secondary px-2.5 py-1 text-xs font-medium text-secondary-foreground"
            title={`${chip.ruleCount} guardrail rule(s) fired`}
        >
            <ShieldAlert className="size-3.5" aria-hidden />
            Guardrail: {chip.action}
            <span className="text-muted-foreground">
                ({chip.ruleCount} rule{chip.ruleCount === 1 ? "" : "s"})
            </span>
        </span>
    );
}
