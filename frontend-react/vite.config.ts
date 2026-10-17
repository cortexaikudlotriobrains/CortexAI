import { defineConfig } from "vitest/config";
import react from "@vitejs/plugin-react";

declare const process: {
  env: Record<string, string | undefined>;
};

const apiProxyTarget = process.env.CORTEX_API_PROXY_TARGET ?? "http://localhost:8000";

export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      "/v1": {
        target: apiProxyTarget,
        changeOrigin: true,
      },
      "/auth": {
        target: apiProxyTarget,
        changeOrigin: true,
      },
      "/runtime-config.js": {
        target: apiProxyTarget,
        changeOrigin: true,
      },
    },
  },
  build: {
    outDir: "dist",
    sourcemap: true,
  },
  test: {
    environment: "jsdom",
    globals: true,
    setupFiles: ["./src/test-setup.ts"],
    // Behaviour tests run against a frozen model catalog so registry updates do not
    // rewrite them; src/__tests__/generatedDefaults.test.ts checks the real file.
    alias: [
      {
        find: /^\.\/generated\/registryDefaults$/,
        replacement: "/src/__tests__/fixtures/registryDefaults.fixture.ts",
      },
    ],
  },
});
