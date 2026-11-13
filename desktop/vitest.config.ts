import { defineConfig, mergeConfig } from "vitest/config";
import viteConfig from "./vite.config";

// Test-only configuration: reuses the Vite build config and adds the jsdom environment.
export default mergeConfig(
  viteConfig,
  defineConfig({
    test: {
      environment: "jsdom",
      setupFiles: ["./src/test/setup.ts"],
      include: ["src/**/*.test.{ts,tsx}"],
      restoreMocks: true,
      css: false,
    },
  })
);
