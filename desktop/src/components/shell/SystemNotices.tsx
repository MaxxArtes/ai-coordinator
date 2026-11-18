import React from "react";
import { Icon } from "../ui/Icon";

export interface SystemNotice {
  id: string;
  tone: "error" | "warn";
  title: string;
  body?: React.ReactNode;
  action?: { label: string; onClick: () => void };
}

/**
 * Persistent application state the operator must know about (preferences not loaded, protected or invalid settings,
 * a recovered profile). Unlike NoticeBanner it is never replaced by a transient message nor dismissed: each notice
 * stays while its cause lasts. The live region is always mounted, so a notice added later is announced.
 */
export const SystemNotices: React.FC<{ notices: SystemNotice[] }> = ({ notices }) => (
  <div className="system-notices" aria-live="assertive" aria-relevant="additions text" data-testid="system-notices">
    {notices.map((notice) => (
      <div key={notice.id} className={`notice notice--${notice.tone}`} data-notice={notice.id}>
        <div className="notice__content">
          <span className="notice__icon">
            <Icon name="alert" size="sm" />
          </span>
          <div className="notice__stack">
            <p className="notice__title">{notice.title}</p>
            {notice.body && <p className="notice__body">{notice.body}</p>}
          </div>
        </div>
        {notice.action && (
          <button type="button" className="btn btn--sm" onClick={notice.action.onClick}>
            {notice.action.label}
          </button>
        )}
      </div>
    ))}
  </div>
);
