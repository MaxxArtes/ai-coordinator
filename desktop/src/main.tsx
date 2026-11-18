import React from "react";
import ReactDOM from "react-dom/client";
import { App } from "./App";
import { restoreCachedAppearance } from "./components/settings/api";
import "./index.css";

// Before the first frame: the appearance last applied in this window, so the shell never paints and then jumps.
restoreCachedAppearance();

const rootElement = document.getElementById("root");

if (rootElement) {
  ReactDOM.createRoot(rootElement).render(
    <React.StrictMode>
      <App />
    </React.StrictMode>
  );
}
