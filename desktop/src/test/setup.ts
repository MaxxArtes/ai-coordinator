import "@testing-library/jest-dom/vitest";
import { afterEach } from "vitest";
import { cleanup } from "@testing-library/react";

// Tests marked "@vitest-environment node" (CSS and token analysis) have no DOM to clean.
afterEach(() => {
  if (typeof document === "undefined") return;
  cleanup();
  document.body.innerHTML = "";
});
