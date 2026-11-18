import React from "react";
import { Icon } from "../ui/Icon";

export interface StartupStatusProps {
  state: "connecting" | "failed";
  detail?: string;
  /** Offered on failure: reloading the window is blocked (it would lose terminals), so this is the way to retry. */
  onRetry?: () => void;
}

/**
 * Full-area startup states: connecting (polite status) and failure (alert with the reported detail).
 * Each is an F6 region whose entry is the message itself, so the keyboard can always come back to it.
 */
export const StartupStatus: React.FC<StartupStatusProps> = ({ state, detail, onRetry }) =>
  state === "connecting" ? (
    <div className="screen-center" data-f6-region="view">
      <div className="startup-status" role="status" tabIndex={-1} data-f6-entry="">
        <span className="spinner" aria-hidden="true" />
        <p className="startup-status__title">Inicializando runtime do Core e conectando ponte ConPTY…</p>
      </div>
    </div>
  ) : (
    <div className="screen-center" data-f6-region="view">
      <div className="startup-status startup-status--error" role="alert" tabIndex={-1} data-f6-entry="">
        <Icon name="stop" />
        <p className="startup-status__title">Falha na inicialização da Alpha técnica</p>
        <p className="startup-status__detail">{detail || "Verifique se o runtime Python privado está disponível."}</p>
      </div>
      {onRetry && (
        <button type="button" className="btn btn--sm" onClick={onRetry}>
          Tentar novamente
        </button>
      )}
    </div>
  );
