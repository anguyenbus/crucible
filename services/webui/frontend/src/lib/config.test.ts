/**
 * The guardrails toggle → pipeline config mapping (browser-free).
 *
 * The one rule that decides which pinned config a turn runs under: ON = the
 * guarded default, OFF = the unguarded config. Env pins are read at call time,
 * so these tests drive the defaults (no env override set in the test env).
 */

import { describe, expect, it } from "vitest";
import {
    DEFAULT_CONFIG_REF,
    DEFAULT_UNGUARDED_CONFIG_REF,
    resolveConfigRef,
} from "./config";

describe("resolveConfigRef (guardrails toggle)", () => {
    it("ON → the guarded default config (1.8.0)", () => {
        expect(resolveConfigRef(true)).toBe(DEFAULT_CONFIG_REF);
        expect(DEFAULT_CONFIG_REF).toBe("legal-rag-default-1.8.0");
    });

    it("OFF → the unguarded config (1.2.0)", () => {
        expect(resolveConfigRef(false)).toBe(DEFAULT_UNGUARDED_CONFIG_REF);
        expect(DEFAULT_UNGUARDED_CONFIG_REF).toBe("legal-rag-default-1.2.0");
    });

    it("ON and OFF select different configs", () => {
        expect(resolveConfigRef(true)).not.toBe(resolveConfigRef(false));
    });
});
