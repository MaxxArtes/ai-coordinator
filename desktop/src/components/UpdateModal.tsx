import React, { useState, useEffect, useCallback, useRef } from "react";

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

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/70 backdrop-blur-sm p-4">
      <div className="bg-[#181825] border border-gray-700 rounded-lg shadow-2xl w-full max-w-lg overflow-hidden flex flex-col max-h-[90vh]">
        {/* Modal Header */}
        <div className="bg-[#1e1e2e] px-5 py-3 border-b border-gray-800 flex items-center justify-between">
          <div className="flex items-center space-x-2">
            <span className="text-blue-400 text-lg">🔄</span>
            <h3 className="text-sm font-semibold text-white tracking-wide">
              Configurações / Atualizações do Sistema
            </h3>
          </div>
          <button
            onClick={onClose}
            disabled={isUpdating || runtimeStopped}
            className="text-gray-400 hover:text-white text-sm font-bold disabled:opacity-50"
          >
            ✕
          </button>
        </div>

        {/* Modal Body */}
        <div className="p-5 overflow-y-auto space-y-4 text-xs font-sans text-gray-300">
          {/* Metadata Cards */}
          <div className="grid grid-cols-2 gap-3 bg-[#11111b] p-3 rounded border border-gray-800">
            <div>
              <span className="text-gray-500 block uppercase text-[10px] font-mono">Versão Instalada</span>
              <span className="font-mono text-white text-sm">{currentVersion}</span>
            </div>
            <div>
              <span className="text-gray-500 block uppercase text-[10px] font-mono">Canal de Distribuição</span>
              <span className="font-mono text-purple-400 text-sm uppercase">{channel}</span>
            </div>
          </div>

          {/* Action: Check Updates */}
          <div className="flex items-center justify-between pt-1">
            <span className="text-gray-400">Verifique novas versões no canal {channel}:</span>
            <button
              onClick={handleCheckUpdates}
              disabled={isChecking || isUpdating || runtimeStopped}
              className="px-3 py-1.5 bg-blue-600 hover:bg-blue-500 text-white rounded font-medium disabled:opacity-50 flex items-center space-x-1.5 transition-colors"
            >
              {isChecking ? (
                <>
                  <span className="animate-spin text-xs">⏳</span>
                  <span>Verificando...</span>
                </>
              ) : (
                <span>Verificar Atualizações</span>
              )}
            </button>
          </div>

          {/* Error Message */}
          {errorMessage && (
            <div className="bg-red-950/80 border border-red-800 p-3 rounded text-red-200">
              <div className="font-semibold mb-1">Aviso do Atualizador:</div>
              <div className="text-[11px] font-mono leading-relaxed">{errorMessage}</div>
            </div>
          )}

          {installError && (
            <div className="bg-red-950/80 border border-red-800 p-3 rounded text-red-200">
              <div className="font-semibold mb-1">Falha na instalação:</div>
              <div className="text-[11px] font-mono leading-relaxed">{installError}</div>
            </div>
          )}

          {/* Post-shutdown install failure: runtime and terminals already stopped; only restart is safe */}
          {runtimeStopped && (
            <div className="bg-red-950/60 border border-red-600/70 p-3 rounded space-y-2">
              <div className="text-red-300 font-semibold flex items-center space-x-1">
                <span>🛑</span>
                <span>Runtime Core encerrado — reinício necessário</span>
              </div>
              <p className="text-[11px] text-red-200 leading-tight">
                O runtime Core não está ativo (encerrado para a instalação, que não pôde ser iniciada, ou
                finalizado inesperadamente), e os terminais não podem ser usados neste estado. A versão atual
                ({currentVersion}) continua instalada. Reinicie o aplicativo para restaurar o runtime; a
                atualização poderá ser tentada novamente depois.
              </p>
              <div className="flex justify-end">
                <button
                  onClick={handleRestartApp}
                  disabled={isRestarting || !isTauri}
                  className="px-4 py-1.5 bg-red-700 hover:bg-red-600 text-white rounded text-xs font-semibold disabled:opacity-50"
                >
                  {isRestarting ? "Reiniciando..." : "Reiniciar aplicativo"}
                </button>
              </div>
            </div>
          )}

          {/* Update Check Result */}
          {checkResult && (
            <div className="space-y-3 mt-2">
              {checkResult.available ? (
                <div className="bg-emerald-950/40 border border-emerald-600/50 p-4 rounded space-y-2">
                  <div className="flex items-center justify-between">
                    <span className="font-bold text-emerald-400 text-sm flex items-center space-x-1">
                      <span>✨</span>
                      <span>Nova versão disponível: v{checkResult.latest_version}</span>
                    </span>
                    {checkResult.date && (
                      <span className="text-[10px] text-gray-400 font-mono">
                        {new Date(checkResult.date).toLocaleDateString()}
                      </span>
                    )}
                  </div>

                  {checkResult.notes && (
                    <div className="mt-2 bg-[#11111b] p-3 rounded border border-gray-800 text-[11px] text-gray-300">
                      <span className="text-gray-400 block font-semibold mb-1">Notas de Lançamento:</span>
                      <p className="whitespace-pre-wrap leading-relaxed">{checkResult.notes}</p>
                    </div>
                  )}

                  {/* Fail-closed notice when session inspection is unknown */}
                  {checkResult.sessions_known === false && (
                    <div className="bg-red-950/60 border border-red-600/70 p-3 rounded space-y-1 mt-3">
                      <div className="text-red-300 font-semibold flex items-center space-x-1">
                        <span>🛑</span>
                        <span>Segurança Fail-Closed</span>
                      </div>
                      <p className="text-[11px] text-red-200 leading-tight">
                        Não foi possível verificar com segurança o estado dos terminais ativos ({checkResult.sessions_error || "Erro no Core IPC"}).
                        A atualização está bloqueada preventivamente para proteger seus processos.
                      </p>
                    </div>
                  )}

                  {/* Active sessions safety fence (authoritative count across all tabs) */}
                  {authoritativeSessionsCount > 0 && sessionsKnown && (
                    <div className="bg-amber-950/50 border border-amber-600/60 p-3 rounded space-y-2 mt-3">
                      <div className="text-amber-300 font-semibold flex items-center space-x-1">
                        <span>⚠️</span>
                        <span>
                          Atenção: {authoritativeSessionsCount} terminal(is) ConPTY ativo(s) no Core (todas as abas)
                        </span>
                      </div>
                      {activePanesCount !== authoritativeSessionsCount && (
                        <p className="text-[10px] text-amber-300/80 font-mono">
                          (Painéis na aba visível: {activePanesCount} | Total em execução: {authoritativeSessionsCount})
                        </p>
                      )}
                      <p className="text-[11px] text-amber-200/90 leading-tight">
                        Para proteger a integridade dos seus comandos, o atualizador não encerra
                        processos em execução sem autorização prévia para a contagem atual.
                      </p>
                      <label className="flex items-center space-x-2 pt-1 cursor-pointer">
                        <input
                          type="checkbox"
                          checked={forceAuthorized && authorizedCount === authoritativeSessionsCount}
                          disabled={isChecking || isUpdating}
                          onChange={(e) => {
                            const checked = e.target.checked;
                            setForceAuthorized(checked);
                            setAuthorizedCount(checked ? authoritativeSessionsCount : null);
                          }}
                          className="rounded border-gray-600 text-blue-600 focus:ring-0"
                        />
                        <span className="text-xs text-white font-medium">
                          Autorizo o encerramento de todos os {authoritativeSessionsCount} terminal(is) ativo(s) agora
                        </span>
                      </label>
                    </div>
                  )}

                  {/* Install Action */}
                  <div className="pt-2 flex items-center justify-end space-x-3">
                    <button
                      onClick={onClose}
                      disabled={isUpdating || runtimeStopped}
                      className="px-3 py-1.5 bg-gray-800 hover:bg-gray-700 text-gray-300 rounded text-xs font-medium"
                    >
                      Adiar
                    </button>
                    <button
                      onClick={handleDownloadAndInstall}
                      disabled={
                        isUpdating ||
                        isChecking ||
                        runtimeStopped ||
                        !sessionsKnown ||
                        (authoritativeSessionsCount > 0 &&
                          (!forceAuthorized || authorizedCount !== authoritativeSessionsCount))
                      }
                      className="px-4 py-1.5 bg-emerald-600 hover:bg-emerald-500 text-white rounded text-xs font-semibold shadow-md disabled:opacity-40 disabled:cursor-not-allowed transition-colors"
                    >
                      {isUpdating ? "Atualizando..." : "Atualizar e Reiniciar"}
                    </button>
                  </div>
                </div>
              ) : !checkResult.error && (
                <div className="bg-gray-800/40 border border-gray-700 p-3 rounded text-center text-gray-300">
                  <span className="text-emerald-400 font-medium">✓ O AI Coordinator Desktop está atualizado.</span>
                  <p className="text-[11px] text-gray-400 mt-0.5">Versão instalada ({currentVersion}) é a mais recente no canal {channel}.</p>
                </div>
              )}
            </div>
          )}

          {/* Update Progress In-flight with Real Progress */}
          {isUpdating && (
            <div className="bg-blue-950/60 border border-blue-800 p-3.5 rounded text-blue-200 space-y-2.5">
              <div className="flex items-center justify-between text-xs font-semibold">
                <span className="flex items-center space-x-1.5">
                  <span className="animate-spin">⚙️</span>
                  <span>{updateStatus}</span>
                </span>
                <span className="font-mono text-emerald-400">{downloadPercent.toFixed(0)}%</span>
              </div>
              <div className="w-full bg-[#11111b] border border-blue-900 rounded-full h-2 overflow-hidden">
                <div
                  className="bg-emerald-500 h-2 rounded-full transition-all duration-300 shadow-sm"
                  style={{ width: `${Math.max(5, Math.min(100, downloadPercent))}%` }}
                ></div>
              </div>
              {totalBytes > 0 && (
                <div className="flex justify-between text-[10px] text-gray-400 font-mono">
                  <span>Transferido: {(downloadedBytes / (1024 * 1024)).toFixed(1)} MB</span>
                  <span>Total: {(totalBytes / (1024 * 1024)).toFixed(1)} MB</span>
                </div>
              )}
            </div>
          )}
        </div>

        {/* Modal Footer */}
        <div className="bg-[#1e1e2e] px-5 py-2.5 border-t border-gray-800 flex items-center justify-between text-[11px] text-gray-400 font-mono">
          <span>Assinatura Minisign / Ed25519 ativa</span>
          <span>Canal {channel.toUpperCase()}</span>
        </div>
      </div>
    </div>
  );
};
