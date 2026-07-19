import { defineConfig } from "vitest/config";
import { fileURLToPath } from "node:url";

export default defineConfig({
    test: {
        // Browser-free logic tests only (mirrors demo_ui's pytest pattern): the
        // SSE parser, envelope->render mapping, citation numbering, history
        // windowing, and the proxy handlers all run under the Node environment.
        environment: "node",
        include: ["src/**/*.test.ts"],
    },
    resolve: {
        alias: {
            "@": fileURLToPath(new URL("./src", import.meta.url)),
        },
    },
});
