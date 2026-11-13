import React from "react";
import { Icon } from "../ui/Icon";

export type NoticeTone = "error" | "success" | "info";

export interface NoticeBannerProps {
  message: string;
  tone?: NoticeTone;
  onDismiss: () => void;
}

const ICON_BY_TONE = { error: "alert", success: "check", info: "info" } as const;

/** Application-level message. Errors are announced assertively (role="alert"), others politely. */
export const NoticeBanner: React.FC<NoticeBannerProps> = ({ message, tone = "error", onDismiss }) => (
  <div
    className={`notice notice--${tone}`}
    role={tone === "error" ? "alert" : "status"}
  >
    <div className="notice__content">
      <span className="notice__icon">
        <Icon name={ICON_BY_TONE[tone]} size="sm" />
      </span>
      <p className="notice__text">{message}</p>
    </div>
    <button type="button" onClick={onDismiss} className="btn btn--ghost btn--icon btn--sm" aria-label="Dispensar aviso">
      <Icon name="close" size="sm" />
    </button>
  </div>
);
