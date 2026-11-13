import React from "react";
import type { ConnectionStatus, RuntimeStatus } from "../shell/AppHeader";
import { UpdatesSection } from "./UpdatesSection";

export interface AboutPanelProps {
  active: boolean;
  isTauri: boolean;
  runtimeStatus: RuntimeStatus | null;
  connectionStatus: ConnectionStatus;
  activePanesCount: number;
  callTauri: (cmd: string, args?: Record<string, any>) => Promise<any>;
  onRequestClose: () => void;
  onUpdatesLockChange: (locked: boolean) => void;
}

const CONNECTION_TEXT: Record<ConnectionStatus, string> = {
  connected: "Ativa",
  authorizing: "Aguardando autorização de projeto",
  connecting: "Conectando",
  failed: "Falha",
};

const UNAVAILABLE = "indisponível";

/** Configurações → Sobre: real versions, license and diagnostics, plus the migrated update flow. */
export const AboutPanel: React.FC<AboutPanelProps> = ({
  active,
  isTauri,
  runtimeStatus,
  connectionStatus,
  activePanesCount,
  callTauri,
  onRequestClose,
  onUpdatesLockChange,
}) => {
  const rs = runtimeStatus;
  return (
    <>
      <div className="settings__panel-header">
        <h3 className="settings__heading">Sobre</h3>
        {!isTauri && <span className="badge badge--warn">Modo preview: dados simulados</span>}
      </div>

      <section aria-labelledby="about-versions-title">
        <h4 id="about-versions-title" className="settings__section-title">
          Versões
        </h4>
        <dl className="kv">
          <dt>Aplicativo</dt>
          <dd>{rs?.app_version || UNAVAILABLE}</dd>
          <dt>Core</dt>
          <dd>{rs?.core_version || UNAVAILABLE}</dd>
          <dt>Python do runtime</dt>
          <dd>{rs?.python_version || UNAVAILABLE}</dd>
          <dt>Sistema</dt>
          <dd>{rs ? [rs.host_os, rs.host_arch].filter(Boolean).join(" / ") : UNAVAILABLE}</dd>
        </dl>
      </section>

      <section aria-labelledby="about-license-title">
        <h4 id="about-license-title" className="settings__section-title">
          Licença
        </h4>
        <dl className="kv">
          <dt>Identificador</dt>
          <dd>{rs?.license || UNAVAILABLE}</dd>
        </dl>
        <p className="settings__note">Uso não comercial, conforme o arquivo LICENSE distribuído com o projeto.</p>
      </section>

      <section aria-labelledby="about-diagnostics-title">
        <h4 id="about-diagnostics-title" className="settings__section-title">
          Diagnóstico
        </h4>
        <dl className="kv">
          <dt>Supervisão ConPTY</dt>
          <dd>{CONNECTION_TEXT[connectionStatus]}</dd>
          <dt>Runtime Python</dt>
          <dd>{rs ? (rs.is_isolated ? "Privado e isolado" : "Indisponível ou do host") : UNAVAILABLE}</dd>
          <dt>Caminho do runtime</dt>
          <dd>{rs?.python_path || UNAVAILABLE}</dd>
          <dt>Terminais na aba</dt>
          <dd>{activePanesCount}</dd>
        </dl>
      </section>

      <UpdatesSection
        active={active}
        isTauri={isTauri}
        callTauri={callTauri}
        activePanesCount={activePanesCount}
        onRequestClose={onRequestClose}
        onLockChange={onUpdatesLockChange}
      />
    </>
  );
};
