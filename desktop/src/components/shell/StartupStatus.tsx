import React from "react";
import { Icon } from "../ui/Icon";

export interface StartupStatusProps {
  state: "connecting" | "failed";
  detail?: string;
}

/** Full-area startup states: connecting (polite status) and failure (alert with the reported detail). */
export const StartupStatus: React.FC<StartupStatusProps> = ({ state, detail }) =>
  state === "connecting" ? (
    <div className="screen-center">
      <div className="startup-status" role="status">
        <span className="spinner" aria-hidden="true" />
        <p className="startup-status__title">Inicializando runtime do Core e conectando ponte ConPTY…</p>
      </div>
    </div>
  ) : (
    <div className="screen-center">
      <div className="startup-status startup-status--error" role="alert">
        <Icon name="stop" />
        <p className="startup-status__title">Falha na inicialização da Alpha técnica</p>
        <p className="startup-status__detail">{detail || "Verifique se o runtime Python privado está disponível."}</p>
      </div>
    </div>
  );
