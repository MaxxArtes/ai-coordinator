import React from "react";

/**
 * Brand mark ">_" drawn as inline SVG (the CSP allows no image file nor data: URL). Decorative: the h1 next to it
 * carries the name. The color comes from currentColor, so it follows every theme.
 */
export const BrandMark: React.FC = () => (
  <span className="brand__mark" aria-hidden="true">
    <svg
      viewBox="0 0 20 16"
      fill="none"
      stroke="currentColor"
      strokeWidth={2}
      strokeLinecap="round"
      strokeLinejoin="round"
      focusable="false"
    >
      <path d="M4 4.5 7.75 8 4 11.5" />
      <path d="M10.5 12H16" />
    </svg>
  </span>
);
