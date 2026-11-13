import React, { useState, useEffect, useCallback, useRef } from "react";
import { Dialog } from "./ui/Dialog";
import { Icon } from "./ui/Icon";

export interface UpdateCheckResult {
  available: boolean;
  current_version: string;
  latest_version: string;
  notes: string;
  date: string;
  active_sessions_count: number;
  sessions_known?: boolean;
  sessions_error?: string | null;
  offline: boolean;
  error?: string | null;
  runtime_stopped?: boolean;
}

interface UpdateModalProps {
  isOpen: boolean;
  onClose: () => void;
  activePanesCount: number;
  isTauri: boolean;
  callTauri: (cmd: string, args?: Record<string, any>) => Promise<any>;
}

export const UpdateModal: React.FC<UpdateModalProps> = ({
  isOpen,
  onClose,
  activePanesCount,
  isTauri,
  callTauri,
}) => {
  const [currentVersion, setCurrentVersion] = useState<string>("0.6.0");
  const [channel, setChannel] = useState<string>("alpha");
  const [isChecking, setIsChecking] = useState<boolean>(false);
  const [checkResult, setCheckResult] = useState<UpdateCheckResult | null>(null);
  const [isUpdating, setIsUpdating] = useState<boolean>(false);
  const [updateStatus, setUpdateStatus] = useState<string>("");
  const [downloadPercent, setDownloadPercent] = useState<number>(0);
  const [downloadedBytes, setDownloadedBytes] = useState<number>(0);
  const [totalBytes, setTotalBytes] = useState<number>(0);
  const [errorMessage, setErrorMessage] = useState<string | null>(null);
  // Install failures live in their own state: handleCheckUpdates() clears errorMessage synchronously.
  const [installError, setInstallError] = useState<string | null>(null);
  const [forceAuthorized, setForceAuthorized] = useState<boolean>(false);
  const [authorizedCount, setAuthorizedCount] = useState<number | null>(null);
  // Set when the installer failed AFTER the Core runtime was shut down (runtime_stopped from the host).
  // In that state terminals are gone and the only safe action is restarting the current version.
  const [runtimeStopped, setRuntimeStopped] = useState<boolean>(false);
  const [isRestarting, setIsRestarting] = useState<boolean>(false);

  const checkSeqRef = useRef<number>(0);
  const prevVisiblePanesRef = useRef<number>(activePanesCount);

  const invalidateAuthorization = useCallback(() => {
    setForceAuthorized(false);
    setAuthorizedCount(null);
  }, []);

  const handleCheckUpdates = useCallback(async () => {
    const seq = ++checkSeqRef.current;
    setIsChecking(true);
    setErrorMessage(null);
    invalidateAuthorization();
    try {
      if (isTauri) {
        const res: UpdateCheckResult = await callTauri("updater_check");
        if (seq !== checkSeqRef.current) return;
        setCheckResult(res);
        if (res.runtime_stopped === true) {
          // Host reports the Core runtime is down (also survives a webview reload).
          setRuntimeStopped(true);
        }
        if (res.error) {
          setErrorMessage(res.error);
        }
      } else {
        // Preview mode simulation
        setTimeout(() => {
          if (seq !== checkSeqRef.current) return;
          setCheckResult({
            available: true,
            current_version: currentVersion,
            latest_version: "0.6.1-alpha.50",
            notes: "AI Coordinator Desktop Alpha — Nova versão de teste (simulada em modo preview).",
            date: new Date().toISOString(),
            active_sessions_count: activePanesCount,
            sessions_known: true,
            offline: false,
          });
          setIsChecking(false);
        }, 500);
        return;
      }
    } catch (err: any) {
      if (seq !== checkSeqRef.current) return;
      setErrorMessage(`Falha na verificação de atualização: ${err?.message || err}`);
    } finally {
      if (seq === checkSeqRef.current) {
        setIsChecking(false);
      }
    }
  }, [isTauri, callTauri, activePanesCount, currentVersion, invalidateAuthorization]);

  // Load channel info, trigger fresh authoritative check, and listen for download progress
  useEffect(() => {
    if (!isOpen) {
      invalidateAuthorization();
      setInstallError(null);
      return;
    }

    let unlistenFn: (() => void) | null = null;
    prevVisiblePanesRef.current = activePanesCount;
    handleCheckUpdates();

    if (isTauri) {
      callTauri("updater_get_channel_info")
        .then((info) => {
          if (info) {
            setCurrentVersion(info.current_version || "0.6.1-alpha.50");
            setChannel(info.channel || "alpha");
          }
        })
        .catch(() => {
          // Fallback
        });

      import("@tauri-apps/api/event").then(({ listen }) => {
        listen("updater-progress", (event: any) => {
          const payload = event.payload;
          if (payload) {
            if (typeof payload.percent === "number") {
              setDownloadPercent(payload.percent);
            }
            if (typeof payload.downloaded === "number") {
              setDownloadedBytes(payload.downloaded);
            }
            if (typeof payload.total === "number") {
              setTotalBytes(payload.total);
            }
            if (payload.status === "downloading") {
              const dlMb = (payload.downloaded / (1024 * 1024)).toFixed(1);
              const totMb = (payload.total / (1024 * 1024)).toFixed(1);
              setUpdateStatus(`Baixando: ${dlMb} MB de ${totMb} MB (${payload.percent.toFixed(0)}%)`);
            } else if (payload.status === "installing") {
              setUpdateStatus("Assinatura Ed25519 validada. Executando o instalador...");
            }
          }
        }).then((unlisten) => {
          unlistenFn = unlisten;
        }).catch(() => {});
      }).catch(() => {});
    }

    return () => {
      if (unlistenFn) unlistenFn();
    };
  }, [isOpen, isTauri, callTauri]);

  // Invalidate prior authorization and refresh authoritative count if visible panes change while modal is open
  useEffect(() => {
    if (!isOpen || isUpdating || runtimeStopped) return;
    if (activePanesCount !== prevVisiblePanesRef.current) {
      prevVisiblePanesRef.current = activePanesCount;
      invalidateAuthorization();
      handleCheckUpdates();
    }
  }, [activePanesCount, isOpen, isUpdating, runtimeStopped, invalidateAuthorization, handleCheckUpdates]);

  // Invalidate prior authorization if checkResult state or count changed
  useEffect(() => {
    if (!checkResult || checkResult.sessions_known === false) {
      if (forceAuthorized || authorizedCount !== null) {
        invalidateAuthorization();
      }
      return;
    }
    if (authorizedCount !== null && checkResult.active_sessions_count !== authorizedCount) {
      invalidateAuthorization();
    }
  }, [checkResult, forceAuthorized, authorizedCount, invalidateAuthorization]);

  const sessionsKnown = checkResult !== null && checkResult.sessions_known !== false;
  const authoritativeSessionsCount = checkResult ? checkResult.active_sessions_count : 0;

  const handleDownloadAndInstall = useCallback(async () => {
    if (!checkResult || checkResult.sessions_known === false) {
      invalidateAuthorization();
      setErrorMessage("Atualização bloqueada: estado das sessões é indefinido/desconhecido (fail-closed).");
      return;
    }

    const expectedCount = checkResult.active_sessions_count;
    if (expectedCount > 0 && (!forceAuthorized || authorizedCount !== expectedCount)) {
      invalidateAuthorization();
      setErrorMessage(
        `Existem ${expectedCount} terminais ativos no Core (em todas as abas). Marque a caixa de confirmação para autorizar o encerramento seguro antes de prosseguir.`
      );
      return;
    }

    setIsUpdating(true);
    setErrorMessage(null);
    setInstallError(null);
    setUpdateStatus("Baixando atualização assinada e verificando autenticidade...");

    try {
      if (isTauri) {
        await callTauri("updater_download_and_install", {
          force: forceAuthorized,
          expectedActiveSessions: forceAuthorized ? expectedCount : null,
        });
        setUpdateStatus("Atualização concluída com sucesso. Reiniciando o aplicativo...");
      } else {
        // Preview mode simulation
        setUpdateStatus("Simulação: download e validação criptográfica aprovados.");
        setTimeout(() => {
          setIsUpdating(false);
          setUpdateStatus("Atualização simulada com sucesso.");
        }, 1200);
      }
    } catch (err: any) {
      setIsUpdating(false);
      invalidateAuthorization();
      setUpdateStatus("");
      setInstallError(`Erro ao instalar atualização: ${err?.message || err}`);
      if (err?.runtime_stopped === true) {
        // Core runtime is down (stopped for install or exited): a re-check could only report an unknown state.
        setRuntimeStopped(true);
        return;
      }
      handleCheckUpdates();
    }
  }, [checkResult, forceAuthorized, authorizedCount, isTauri, callTauri, invalidateAuthorization, handleCheckUpdates]);

  const handleRestartApp = useCallback(async () => {
    setIsRestarting(true);
    try {
      await callTauri("updater_restart_app");
    } catch (err: any) {
      setIsRestarting(false);
      setInstallError(`Falha ao reiniciar o aplicativo: ${err?.message || err}. Feche e abra o AI Coordinator Desktop manualmente.`);
    }
  }, [callTauri]);

  if (!isOpen) return null;

  // Closing is blocked while an install runs or after the runtime was stopped (restart is the only path).
  const dismissible = !isUpdating && !runtimeStopped;

  return (
    <Dialog
      open
      title="Atualizações do sistema"
      onClose={onClose}
      dismissible={dismissible}
      closeLabel="Fechar atualizações"
      footer={
        <>
          <span>Assinatura Minisign / Ed25519 ativa</span>
          <span>Canal {channel.toUpperCase()}</span>
        </>
      }
    >
      {/* Metadata */}
      <dl className="meta-grid">
        <div>
          <dt>Versão instalada</dt>
          <dd>{currentVersion}</dd>
        </div>
        <div>
          <dt>Canal de distribuição</dt>
          <dd className="meta-grid__channel">{channel}</dd>
        </div>
      </dl>

      {/* Action: Check Updates */}
      <div className="row-between">
        <span>Verifique novas versões no canal {channel}:</span>
        <button
          type="button"
          onClick={handleCheckUpdates}
          disabled={isChecking || isUpdating || runtimeStopped}
          className="btn btn--primary"
        >
          {isChecking ? (
            <>
              <span className="spinner" aria-hidden="true" />
              <span>Verificando…</span>
            </>
          ) : (
            <span>Verificar atualizações</span>
          )}
        </button>
      </div>

      {/* Check error */}
      {errorMessage && (
        <div className="callout callout--error" role="alert">
          <p className="callout__title">
            <Icon name="alert" size="sm" />
            <span>Aviso do atualizador</span>
          </p>
          <p className="callout__body callout__body--mono">{errorMessage}</p>
        </div>
      )}

      {/* Install error (kept apart from check errors) */}
      {installError && (
        <div className="callout callout--error" role="alert">
          <p className="callout__title">
            <Icon name="alert" size="sm" />
            <span>Falha na instalação</span>
          </p>
          <p className="callout__body callout__body--mono">{installError}</p>
        </div>
      )}

      {/* Post-shutdown install failure: runtime and terminals already stopped; only restart is safe */}
      {runtimeStopped && (
        <div className="callout callout--error">
          <p className="callout__title">
            <Icon name="stop" size="sm" />
            <span>Runtime Core encerrado — reinício necessário</span>
          </p>
          <p className="callout__body">
            O runtime Core não está ativo (encerrado para a instalação, que não pôde ser iniciada, ou finalizado
            inesperadamente), e os terminais não podem ser usados neste estado. A versão atual ({currentVersion})
            continua instalada. Reinicie o aplicativo para restaurar o runtime; a atualização poderá ser tentada
            novamente depois.
          </p>
          <div className="row-end">
            <button
              type="button"
              onClick={handleRestartApp}
              disabled={isRestarting || !isTauri}
              className="btn btn--danger"
            >
              {isRestarting ? "Reiniciando…" : "Reiniciar aplicativo"}
            </button>
          </div>
        </div>
      )}

      {/* Update Check Result (hidden while the runtime is down: restarting is the only valid action) */}
      {checkResult &&
        !runtimeStopped &&
        (checkResult.available ? (
          <section className="update-card">
            <div className="row-between">
              <h3 className="update-card__title">
                <Icon name="update" size="sm" />
                <span>Nova versão disponível: v{checkResult.latest_version}</span>
              </h3>
              {checkResult.date && (
                <span className="update-card__date">{new Date(checkResult.date).toLocaleDateString()}</span>
              )}
            </div>

            {checkResult.notes && (
              <div className="release-notes">
                <span className="release-notes__title">Notas de lançamento</span>
                <p className="release-notes__text">{checkResult.notes}</p>
              </div>
            )}

            {/* Fail-closed notice when session inspection is unknown */}
            {checkResult.sessions_known === false && (
              <div className="callout callout--error" role="alert">
                <p className="callout__title">
                  <Icon name="stop" size="sm" />
                  <span>Segurança fail-closed</span>
                </p>
                <p className="callout__body">
                  Não foi possível verificar com segurança o estado dos terminais ativos (
                  {checkResult.sessions_error || "Erro no Core IPC"}). A atualização está bloqueada preventivamente
                  para proteger seus processos.
                </p>
              </div>
            )}

            {/* Active sessions safety fence (authoritative count across all tabs) */}
            {authoritativeSessionsCount > 0 && sessionsKnown && (
              <div className="callout callout--warn">
                <p className="callout__title">
                  <Icon name="alert" size="sm" />
                  <span>
                    Atenção: {authoritativeSessionsCount} terminal(is) ConPTY ativo(s) no Core (todas as abas)
                  </span>
                </p>
                {activePanesCount !== authoritativeSessionsCount && (
                  <p className="callout__body callout__body--mono">
                    (Painéis na aba visível: {activePanesCount} | Total em execução: {authoritativeSessionsCount})
                  </p>
                )}
                <p className="callout__body">
                  Para proteger a integridade dos seus comandos, o atualizador não encerra processos em execução sem
                  autorização prévia para a contagem atual.
                </p>
                <label className="checkbox-row">
                  <input
                    type="checkbox"
                    checked={forceAuthorized && authorizedCount === authoritativeSessionsCount}
                    disabled={isChecking || isUpdating}
                    onChange={(e) => {
                      const checked = e.target.checked;
                      setForceAuthorized(checked);
                      setAuthorizedCount(checked ? authoritativeSessionsCount : null);
                    }}
                  />
                  <span>Autorizo o encerramento de todos os {authoritativeSessionsCount} terminal(is) ativo(s) agora</span>
                </label>
              </div>
            )}

            {/* Install Action */}
            <div className="row-end">
              <button type="button" onClick={onClose} disabled={isUpdating || runtimeStopped} className="btn">
                Adiar
              </button>
              <button
                type="button"
                onClick={handleDownloadAndInstall}
                disabled={
                  isUpdating ||
                  isChecking ||
                  runtimeStopped ||
                  !sessionsKnown ||
                  (authoritativeSessionsCount > 0 &&
                    (!forceAuthorized || authorizedCount !== authoritativeSessionsCount))
                }
                className="btn btn--primary"
              >
                {isUpdating ? "Atualizando…" : "Atualizar e reiniciar"}
              </button>
            </div>
          </section>
        ) : (
          !checkResult.error && (
            <div className="callout callout--ok callout--center" role="status">
              <p className="callout__title">
                <Icon name="check" size="sm" />
                <span>O AI Coordinator Desktop está atualizado.</span>
              </p>
              <p className="callout__body">
                Versão instalada ({currentVersion}) é a mais recente no canal {channel}.
              </p>
            </div>
          )
        ))}

      {/* Update Progress In-flight with Real Progress */}
      {isUpdating && (
        <div className="callout callout--info">
          <div className="row-between">
            <p className="callout__title" aria-live="polite">
              <span className="spinner" aria-hidden="true" />
              <span>{updateStatus}</span>
            </p>
            <span className="text-mono">{downloadPercent.toFixed(0)}%</span>
          </div>
          <div
            className="progress"
            role="progressbar"
            aria-label="Progresso do download da atualização"
            aria-valuemin={0}
            aria-valuemax={100}
            aria-valuenow={Math.round(Math.max(0, Math.min(100, downloadPercent)))}
          >
            <div className="progress__bar" style={{ width: `${Math.max(5, Math.min(100, downloadPercent))}%` }} />
          </div>
          {totalBytes > 0 && (
            <div className="progress-meta">
              <span>Transferido: {(downloadedBytes / (1024 * 1024)).toFixed(1)} MB</span>
              <span>Total: {(totalBytes / (1024 * 1024)).toFixed(1)} MB</span>
            </div>
          )}
        </div>
      )}
    </Dialog>
  );
};
