import React from "react";

/**
 * Decorative line icons (replace the emoji glyphs previously used as icons).
 * Always aria-hidden: the meaning must also be present as text next to the icon.
 */
export type IconName = "refresh" | "update" | "shield" | "alert" | "stop" | "close" | "check" | "info";

const PATHS: Record<IconName, React.ReactNode> = {
  refresh: (
    <>
      <path d="M13.5 8a5.5 5.5 0 1 1-1.61-3.89" />
      <path d="M13.5 2.5v3h-3" />
    </>
  ),
  update: (
    <>
      <circle cx="8" cy="8" r="5.5" />
      <path d="M8 10.5v-5M5.75 7.75 8 5.5l2.25 2.25" />
    </>
  ),
  shield: <path d="M8 1.75 2.75 3.75v4c0 3 2.25 5.25 5.25 6.5 3-1.25 5.25-3.5 5.25-6.5v-4z" />,
  alert: (
    <>
      <path d="M8 2 1.75 13.25h12.5z" />
      <path d="M8 6.5v3M8 11.25v.01" />
    </>
  ),
  stop: (
    <>
      <path d="M5.25 1.75h5.5l3.5 3.5v5.5l-3.5 3.5h-5.5l-3.5-3.5v-5.5z" />
      <path d="M8 5v3.5M8 10.75v.01" />
    </>
  ),
  close: <path d="M4 4l8 8M12 4l-8 8" />,
  check: (
    <>
      <circle cx="8" cy="8" r="5.5" />
      <path d="M5.5 8.25 7.25 10l3.25-3.5" />
    </>
  ),
  info: (
    <>
      <circle cx="8" cy="8" r="5.5" />
      <path d="M8 7.25v3.5M8 5.25v.01" />
    </>
  ),
};

export const Icon: React.FC<{ name: IconName; size?: "sm" | "md" }> = ({ name, size = "md" }) => (
  <svg
    className={size === "sm" ? "icon icon--sm" : "icon"}
    viewBox="0 0 16 16"
    fill="none"
    stroke="currentColor"
    strokeWidth={1.5}
    strokeLinecap="round"
    strokeLinejoin="round"
    aria-hidden="true"
    focusable="false"
  >
    {PATHS[name]}
  </svg>
);
