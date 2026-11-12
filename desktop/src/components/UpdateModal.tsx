import React, { useState, useEffect, useCallback } from "react";

export interface UpdateCheckResult {
  available: boolean;
  current_version: string;
  latest_version: string;
  notes: string;
  date: string;
  active_sessions_count: number;
  offline: boolean;
  error?: string | null;
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
  const [errorMessage, setErrorMessage] = useState<string | null>(null);
  const [forceAuthorized, setForceAuthorized] = useState<boolean>(false);

  // Load channel info on mount or open
  useEffect(() => {
    if (!isOpen) return;

    if (isTauri) {
      callTauri("updater_get_channel_info")
        .then((info) => {
          if (info) {
            setCurrentVersion(info.current_version || "0.6.0");
            setChannel(info.channel || "alpha");
          }
        })
        .catch(() => {
          // Fallback
        });
    }
  }, [isOpen, isTauri, callTauri]);

  const handleCheckUpdates = useCallback(async () => {
    setIsChecking(true);
    setErrorMessage(null);
    try {
      if (isTauri) {
        const res: UpdateCheckResult = await callTauri("updater_check");
        setCheckResult(res);
        if (res.error) {
          setErrorMessage(res.error);
        }
      } else {
        // Preview mode simulation
        setTimeout(() => {
          setCheckResult({
            available: true,
            current_version: "0.6.0",
            latest_version: "0.6.1-alpha.49",
            notes: "AI Coordinator Desktop Alpha — Nova versão de teste (simulada em modo preview).",
            date: new Date().toISOString(),
            active_sessions_count: activePanesCount,
            offline: false,
          });
          setIsChecking(false);
        }, 500);
        return;
      }
    } catch (err: any) {
      setErrorMessage(`Falha na verificação de atualização: ${err?.message || err}`);
    } finally {
      setIsChecking(false);
    }
  }, [isTauri, callTauri, activePanesCount]);

  const handleDownloadAndInstall = useCallback(async () => {
    if (activePanesCount > 0 && !forceAuthorized) {
      setErrorMessage(
        `Existem ${activePanesCount} terminais ativos em execução. Marque a caixa de confirmação para autorizar o encerramento seguro antes de prosseguir.`
      );
      return;
    }

    setIsUpdating(true);
    setErrorMessage(null);
    setUpdateStatus("Baixando atualização assinada e verificando autenticidade...");

    try {
      if (isTauri) {
        await callTauri("updater_download_and_install", { force: forceAuthorized });
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
      setErrorMessage(`Erro ao instalar atualização: ${err?.message || err}`);
      setUpdateStatus("");
    }
  }, [activePanesCount, forceAuthorized, isTauri, callTauri]);

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
            disabled={isUpdating}
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
              disabled={isChecking || isUpdating}
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

                  {/* Active sessions safety fence */}
                  {activePanesCount > 0 && (
                    <div className="bg-amber-950/50 border border-amber-600/60 p-3 rounded space-y-2 mt-3">
                      <div className="text-amber-300 font-semibold flex items-center space-x-1">
                        <span>⚠️</span>
                        <span>Atenção: {activePanesCount} terminal(is) ConPTY ativo(s)</span>
                      </div>
                      <p className="text-[11px] text-amber-200/90 leading-tight">
                        Para proteger a integridade dos seus comandos, o atualizador não encerra
                        processos em execução sem autorização prévia.
                      </p>
                      <label className="flex items-center space-x-2 pt-1 cursor-pointer">
                        <input
                          type="checkbox"
                          checked={forceAuthorized}
                          onChange={(e) => setForceAuthorized(e.target.checked)}
                          className="rounded border-gray-600 text-blue-600 focus:ring-0"
                        />
                        <span className="text-xs text-white font-medium">
                          Autorizo o encerramento dos terminais para aplicar a atualização agora
                        </span>
                      </label>
                    </div>
                  )}

                  {/* Install Action */}
                  <div className="pt-2 flex items-center justify-end space-x-3">
                    <button
                      onClick={onClose}
                      disabled={isUpdating}
                      className="px-3 py-1.5 bg-gray-800 hover:bg-gray-700 text-gray-300 rounded text-xs font-medium"
                    >
                      Adiar
                    </button>
                    <button
                      onClick={handleDownloadAndInstall}
                      disabled={isUpdating || (activePanesCount > 0 && !forceAuthorized)}
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

          {/* Update Progress In-flight */}
          {isUpdating && (
            <div className="bg-blue-950/60 border border-blue-800 p-3 rounded text-blue-200 space-y-2">
              <div className="flex items-center space-x-2 text-xs font-semibold">
                <span className="animate-spin">⚙️</span>
                <span>{updateStatus}</span>
              </div>
              <div className="w-full bg-blue-900/50 rounded-full h-1.5 overflow-hidden">
                <div className="bg-blue-400 h-1.5 rounded-full animate-pulse w-3/4"></div>
              </div>
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
