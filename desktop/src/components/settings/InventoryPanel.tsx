/**
 * Configurações → Agentes, Provedores (rotas de fallback) and Segurança (Marco 4D.3, Etapa 7).
 * - Read-only discovery: names, availability and where the data came from. Nothing is started, nothing is changed,
 *   and no configuration value is ever shown (K6). What could not be read is said, never shown as an empty list.
 * - Security policy in plain Portuguese (K7): a label and yes/no or a description, never the internal keys.
 * - Revoking an authorized directory is the only mutation: explicit confirmation, focus kept inside Configurações,
 *   and the App is told, so a revoked open project is not used again (K4).
 */
import React, { useEffect, useId, useRef, useState } from "react";
import {
  errorText,
  getAgents,
  getRoutes,
  getSecurityStatus,
  revokeProject,
  type AgentsSnapshot,
  type CallTauri,
  type RevokeResult,
  type RoutesSnapshot,
  type SecurityStatus,
} from "./api";

type InventoryKind = "agentes" | "provedores" | "seguranca";
type InventoryData = AgentsSnapshot | RoutesSnapshot | SecurityStatus;

const LOADERS: Record<InventoryKind, (callTauri: CallTauri) => Promise<InventoryData>> = {
  agentes: getAgents,
  provedores: getRoutes,
  seguranca: getSecurityStatus,
};

const TITLE: Record<InventoryKind, string> = { agentes: "Agentes", provedores: "Rotas de fallback", seguranca: "Segurança" };

/** Policy items the interface knows how to say. Unknown values or keys are reported as such, never shown raw. */
const POLICY_ITEMS: { key: string; label: string; values?: Record<string, string> }[] = [
  { key: "project_fencing", label: "Só diretórios autorizados podem ser abertos como projeto" },
  {
    key: "human_terminal",
    label: "Permissões do terminal humano",
    values: { operator_permissions: "as do seu usuário do Windows (sem sandbox)" },
  },
  { key: "human_terminal_filesystem_isolation", label: "Isolamento de arquivos do terminal humano" },
  {
    key: "governed_agent",
    label: "Agentes governados abertos pelo Desktop",
    values: {
      core_policy: "seguem a política do Core",
      env_sanitized_only: "só recebem um ambiente filtrado (variáveis de segredo removidas); sem sandbox",
      env_sanitized: "só recebem um ambiente filtrado (variáveis de segredo removidas); sem sandbox",
      not_enforced_in_desktop: "nenhuma política é aplicada pelo Desktop",
      operator_permissions: "as do seu usuário do Windows (sem sandbox)",
    },
  },
  { key: "governed_agent_filesystem_isolation", label: "Isolamento de arquivos dos agentes governados" },
];
const POLICY_TEXT_KEYS = new Set(["description"]);

function policyValue(item: (typeof POLICY_ITEMS)[number], value: boolean | string): string {
  if (typeof value === "boolean") return value ? "Sim" : "Não";
  return item.values?.[value] ?? "Valor não reconhecido por esta versão da interface";
}

const SOURCE_TEXT: Record<string, string> = {
  packaged_defaults: "Rotas padrão empacotadas (nenhum routes.json do usuário).",
  configured: "Rotas do routes.json do usuário.",
};

export interface InventoryPanelProps {
  kind: InventoryKind;
  active: boolean;
  isTauri: boolean;
  callTauri: CallTauri;
  /** A revocation went through: the App updates its list and leaves a revoked open project. */
  onAuthorizationChanged?: (result: RevokeResult) => void;
}

export const InventoryPanel: React.FC<InventoryPanelProps> = ({ kind, active, isTauri, callTauri, onAuthorizationChanged }) => {
  const idBase = useId();
  const [data, setData] = useState<InventoryData | null>(null);
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(false);
  const [pending, setPending] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [notice, setNotice] = useState("");
  const cancelRef = useRef<HTMLButtonElement>(null);
  const headingRef = useRef<HTMLHeadingElement>(null);
  // Revogar button that opened the question: Cancelar gives the focus back to it.
  const returnFocusRef = useRef<HTMLElement | null>(null);
  // Every load (and leaving the panel) starts a new generation: an older answer never replaces a newer one.
  const generation = useRef(0);
  const inFlight = useRef(false);
  const pendingRef = useRef<string | null>(null);
  pendingRef.current = pending;
  const changedRef = useRef(onAuthorizationChanged);
  changedRef.current = onAuthorizationChanged;

  const load = async () => {
    const current = ++generation.current;
    setLoading(true);
    setError("");
    try {
      const next = await LOADERS[kind](callTauri);
      if (!next || typeof next !== "object") throw new Error("Resposta de leitura indisponível.");
      if (current === generation.current) setData(next);
    } catch (err) {
      if (current === generation.current) {
        setData(null);
        setError(errorText(err));
      }
    } finally {
      if (current === generation.current) setLoading(false);
    }
  };

  useEffect(() => {
    if (active && isTauri) void load();
    if (!active) {
      generation.current++;
      setLoading(false);
      setPending(null);
    }
    return () => {
      generation.current++;
    };
  }, [kind, active, isTauri, callTauri]);

  useEffect(() => {
    if (pending && active) cancelRef.current?.focus();
  }, [pending, active]);

  const focusAfterClose = (target: HTMLElement | null) => {
    requestAnimationFrame(() => (target && target.isConnected ? target : headingRef.current)?.focus());
  };

  const cancel = () => {
    if (!pendingRef.current || inFlight.current) return;
    setPending(null);
    focusAfterClose(returnFocusRef.current);
    returnFocusRef.current = null;
  };

  // Escape answers the question (Cancelar) from anywhere in Configurações, instead of closing the whole dialog.
  const cancelHandlerRef = useRef(cancel);
  cancelHandlerRef.current = cancel;
  useEffect(() => {
    if (!pending || !active) return;
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key !== "Escape") return;
      event.preventDefault();
      event.stopPropagation();
      cancelHandlerRef.current();
    };
    document.addEventListener("keydown", onKeyDown, true);
    return () => document.removeEventListener("keydown", onKeyDown, true);
  }, [pending, active]);

  const revoke = async () => {
    const target = pendingRef.current;
    if (!target || inFlight.current) return;
    inFlight.current = true;
    setBusy(true);
    setError("");
    setNotice("");
    try {
      const result = await revokeProject(callTauri, target);
      setPending(null);
      returnFocusRef.current = null;
      if (result?.revoked !== true) {
        await load();
        setError("A revogação não foi confirmada; a lista foi relida.");
      } else {
        changedRef.current?.(result);
        await load();
        setNotice(`Autorização de ${target} revogada. Arquivos e histórico foram preservados.`);
      }
      focusAfterClose(headingRef.current);
    } catch (err) {
      // Refused (for instance terminals still open there): the question stays, with the safe answer focused.
      setError(errorText(err));
      cancelRef.current?.focus();
    } finally {
      setBusy(false);
      inFlight.current = false;
    }
  };

  const title = TITLE[kind];
  const locked = busy || loading || pending !== null;
  const status = loading ? "Carregando…" : busy ? "Revogando…" : notice;

  const renderAgents = (agents: AgentsSnapshot) => (
    <>
      {agents.source === "unreadable" && (
        <p className="settings__note">Configuração do Core ilegível: a disponibilidade dos launchers não foi verificada.</p>
      )}
      {agents.source === "packaged_defaults" && (
        <p className="settings__note">Launchers conferidos pela configuração padrão empacotada (nenhum routes.json do usuário).</p>
      )}
      <ul>
        {(agents.agents ?? []).map((agent) => (
          <li key={agent.client ?? agent.name}>
            <strong>{agent.name ?? agent.client}</strong>:{" "}
            {agent.available === true ? "disponível" : agent.available === false ? "indisponível" : "estado desconhecido"}
            {agent.reason ? ` — ${agent.reason}` : ""}
          </li>
        ))}
      </ul>
      {(agents.unsupported ?? []).map((item) => (
        <p key={item.name}>
          {item.name}: não suportado. {item.reason}
        </p>
      ))}
      <h4>Servidores MCP (somente nomes)</h4>
      {agents.mcp_complete === false && (
        <p className="settings__note">Lista de MCP incompleta: alguma fonte não pôde ser lida.</p>
      )}
      {(agents.mcp_servers ?? []).length > 0 ? (
        <ul>
          {agents.mcp_servers.map((name) => (
            <li key={name}>{name}</li>
          ))}
        </ul>
      ) : agents.mcp_complete === false ? (
        <p>Nenhum nome de servidor MCP pôde ser confirmado nas fontes legíveis.</p>
      ) : (
        <p>Nenhum servidor MCP encontrado nas fontes consultadas.</p>
      )}
    </>
  );

  const renderRoutes = (routes: RoutesSnapshot) => (
    <>
      <p>
        Cotas: indisponíveis até a {routes.quota_milestone || "4D.4.3"}. Credenciais armazenadas não comprovam conexão
        ou saldo.
      </p>
      {routes.available === false ? (
        <p role="alert">{routes.reason || "Configuração de rotas ilegível; nada foi alterado."}</p>
      ) : (
        <>
          {SOURCE_TEXT[routes.source] && <p className="settings__note">{SOURCE_TEXT[routes.source]}</p>}
          {(routes.routes ?? []).length === 0 ? (
            <p>Nenhuma rota de fallback configurada.</p>
          ) : (
            <ul>
              {routes.routes.map((route) => (
                <li key={route.category}>
                  <strong>{route.category}</strong>:{" "}
                  {(route.candidates ?? [])
                    .map((candidate) => [candidate.client, candidate.model, candidate.effort].filter(Boolean).join(" / "))
                    .join(" → ") || "sem candidatos configurados"}
                </li>
              ))}
            </ul>
          )}
        </>
      )}
    </>
  );

  const renderSecurity = (security: SecurityStatus) => {
    const policy = security.policy ?? {};
    const known = POLICY_ITEMS.filter((item) => item.key in policy);
    const unknown = Object.keys(policy).filter((key) => !POLICY_TEXT_KEYS.has(key) && !POLICY_ITEMS.some((item) => item.key === key));
    const description = typeof policy.description === "string" ? policy.description : "";
    const projects = security.authorized_projects ?? [];
    return (
      <>
        <h4>Política de acesso (somente leitura)</h4>
        {description && <p>{description}</p>}
        <dl>
          {known.map((item) => (
            <React.Fragment key={item.key}>
              <dt>{item.label}</dt>
              <dd>{policyValue(item, policy[item.key])}</dd>
            </React.Fragment>
          ))}
        </dl>
        {unknown.length > 0 && (
          <p className="settings__note">
            {unknown.length === 1
              ? "1 item da política não é reconhecido por esta versão da interface."
              : `${unknown.length} itens da política não são reconhecidos por esta versão da interface.`}
          </p>
        )}
        <h4 ref={headingRef} tabIndex={-1}>
          Diretórios autorizados
        </h4>
        {projects.length === 0 && <p>Nenhum diretório autorizado.</p>}
        <ul>
          {projects.map((path) => (
            <li key={path}>
              <span className="settings__path">{path}</span>{" "}
              <button
                type="button"
                className="btn btn--sm btn--ghost-danger"
                aria-disabled={locked || undefined}
                onClick={(event) => {
                  if (locked || inFlight.current) return;
                  returnFocusRef.current = event.currentTarget;
                  setNotice("");
                  setError("");
                  setPending(path);
                }}
              >
                Revogar {path}
              </button>
              {pending === path && (
                <div
                  className="callout callout--warn"
                  role="alertdialog"
                  aria-labelledby={`${idBase}-revoke-title`}
                  aria-describedby={`${idBase}-revoke-body`}
                >
                  <p id={`${idBase}-revoke-title`} className="callout__title">
                    <span>Revogar a autorização de {pending}?</span>
                  </p>
                  <p id={`${idBase}-revoke-body`} className="callout__body">
                    Novos terminais deixam de poder abrir este diretório. Arquivos e histórico não são apagados. Feche
                    antes os terminais abertos nele.
                  </p>
                  {/* aria-disabled, never disabled: a real `disabled` drops the focus out of the dialog (WebView2). */}
                  <div className="row-end">
                    <button ref={cancelRef} type="button" className="btn btn--sm" aria-disabled={busy || undefined} onClick={cancel}>
                      Cancelar
                    </button>
                    <button
                      type="button"
                      className="btn btn--sm btn--danger"
                      aria-disabled={busy || undefined}
                      onClick={() => void revoke()}
                    >
                      Confirmar revogação
                    </button>
                  </div>
                </div>
              )}
            </li>
          ))}
        </ul>
      </>
    );
  };

  return (
    <section aria-label={title}>
      <h3 className="settings__heading">{title}</h3>
      <p className="settings__summary">
        {kind === "seguranca"
          ? "Política em vigor e diretórios autorizados. Revogar não apaga arquivos; feche os terminais desse diretório antes."
          : "Somente leitura. Nenhum agente é iniciado e nenhuma rota é alterada nesta tela."}
      </p>
      {!isTauri && <p>Preview: sem acesso ao host. Dados indisponíveis.</p>}
      {error && <p role="alert">{error}</p>}
      {/* Always mounted, so loading, progress and outcome are announced as changes of one live region. */}
      <p className={notice && !loading && !busy ? "settings-form__notice" : "settings-form__notice settings-form__notice--neutral"} role="status">
        {status}
      </p>
      {isTauri && (
        <button
          type="button"
          className="btn btn--sm"
          aria-disabled={locked || undefined}
          onClick={() => {
            if (locked || inFlight.current) return;
            setNotice("");
            void load();
          }}
        >
          Recarregar {title.toLowerCase()}
        </button>
      )}
      {data && kind === "agentes" && renderAgents(data as AgentsSnapshot)}
      {data && kind === "provedores" && renderRoutes(data as RoutesSnapshot)}
      {data && kind === "seguranca" && renderSecurity(data as SecurityStatus)}
    </section>
  );
};
