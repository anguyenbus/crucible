import type { NextConfig } from "next";

const nextConfig: NextConfig = {
    // Frontend-only Phase 1: no custom server, no rewrites. The browser reaches
    // the orchestrator ONLY through the route-handler proxy under app/api/*.
    reactStrictMode: true,
};

export default nextConfig;
