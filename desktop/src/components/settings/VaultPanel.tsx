/**
 * Configurações → Provedores: credentials in the profile vault (Marco 4D.3, Etapa 4; decision D8).
 * - Only key names are ever listed; the secret value is sent once and the field is cleared right after.
 * - Without a secure store (anything but DPAPI on Windows today) no input is offered and nothing is saved.
 * - Stored credentials are not used by any provider integration yet: this is stated, never implied.
 */
import React, { useCallback, useEffect, useId, useState } from "react";
import { Icon } from "../ui/Icon";
import {
  deleteVaultSecret,
  getVaultStatus,
  listVaultKeys,
  setVaultSecret,
  type CallTauri,
  type StructuredError,
  type VaultStatus,
} from "./api";
import type { SettingsCategory } from "./categories";

export interface VaultPanelProps {
  category: SettingsCategory;
  active: boolean;
  isTauri: boolean;
  callTauri: CallTauri;
}

const BACKEND_LABEL: Record<string, string> = { dpapi: "DPAPI do Windows", aes_gcm: "AES-GCM com senha mestra" };

export const VaultPanel: React.FC<VaultPanelProps> = ({ category, active, isTauri, callTauri }) => {
  const formId = useId();
  const [status, setStatus] = useState<VaultStatus | null>(null);
  const [keys, setKeys] = useState<string[]>([]);
  const [keyName, setKeyName] = useState("");
  const [secret, setSecret] = useState("");
  const [error, setError] = useState<StructuredError | null>(null);
  const [notice, setNotice] = useState("");
  const [busy, setBusy] = useState(false);

  const refresh = useCallback(async () => {
    setError(null);
    try {
      const st = await getVaultStatus(callTauri);
      setStatus(st);
      setKeys(st.available ? await listVaultKeys(callTauri) : []);
    } catch (err) {
      setError(err as StructuredError);
    }
  }, [callTauri]);

  useEffect(() => {
    if (active && isTauri) void refresh();
  }, [active, isTauri, refresh]);

  // Never keep a typed secret around when the panel is left.
  useEffect(() => {
    if (!active) setSecret("");
  }, [active]);

  const handleSave = async (event: React.FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    setBusy(true);
    setError(null);
    setNotice("");
    const name = keyName.trim();
    try {
      await setVaultSecret(callTauri, name, secret);
      setSecret("");
      setKeyName("");
      setNotice(`Credencial ${name} gravada no cofre. O valor não é exibido.`);
      setKeys(await listVaultKeys(callTauri));
    } catch (err) {
      setError(err as StructuredError);
    } finally {
      setBusy(false);
    }
  };

  const handleDelete = async (name: string) => {
    setBusy(true);
    setError(null);
    setNotice("");
    try {
      await deleteVaultSecret(callTauri, name);
      setNotice(`Credencial ${name} removida do cofre.`);
      setKeys(await listVaultKeys(callTauri));
    } catch (err) {
      setError(err as StructuredError);
    } finally {
      setBusy(false);
    }
  };

  const unavailable = status !== null && !status.available;
  const keyError = error?.field === "key" ? error : null;
  const valueError = error?.field === "value" ? error : null;
  const generalError = error && !keyError && !valueError ? error : null;

  return (
    <>
      <div className="settings__panel-header">
        <h3 className="settings__heading">{category.label}</h3>
        <div className="settings__badges">
          {status?.available && <span className="badge badge--ok">Cofre: {BACKEND_LABEL[status.backend] ?? status.backend}</span>}
          {unavailable && <span className="badge badge--unsupported">Armazenamento indisponível</span>}
          <span className="badge badge--unsupported">Uso pelos provedores: não suportado nesta versão</span>
        </div>
      </div>
      <p className="settings__summary">{category.summary}</p>

      {!isTauri && (
        <div className="callout callout--info">
          <p className="callout__body">Modo preview: o cofre não é acessado.</p>
        </div>
      )}

      {unavailable && (
        <div className="callout callout--warn" role="status">
          <p className="callout__title">
            <Icon name="shield" size="sm" />
            <span>Armazenamento persistente de credenciais indisponível nesta plataforma</span>
          </p>
          <p className="callout__body">
            Não há cofre seguro do sistema integrado (DPAPI no Windows; Keychain e Secret Service estão planejados
            para a 4D.6). Nenhuma credencial é gravada neste computador.
          </p>
        </div>
      )}

      {status?.available && (
        <>
          <section aria-labelledby={`${formId}-keys`}>
            <h4 id={`${formId}-keys`} className="settings__section-title">
              Credenciais no cofre
            </h4>
            {keys.length === 0 ? (
              <p className="settings__note">Nenhuma credencial gravada.</p>
            ) : (
              <ul className="vault-keys">
                {keys.map((name) => (
                  <li key={name} className="vault-keys__item">
                    <span className="text-mono">{name}</span>
                    <span className="settings__note">valor protegido, não exibido</span>
                    <button
                      type="button"
                      className="btn btn--sm btn--ghost btn--ghost-danger"
                      onClick={() => handleDelete(name)}
                      disabled={busy}
                      aria-label={`Remover a credencial ${name}`}
                    >
                      Remover
                    </button>
                  </li>
                ))}
              </ul>
            )}
          </section>

          <form className="settings-form" onSubmit={handleSave} noValidate aria-labelledby={`${formId}-new`}>
            <h4 id={`${formId}-new`} className="settings__section-title">
              Gravar credencial
            </h4>
            <div className="field">
              <label className="field__label" htmlFor={`${formId}-name`}>
                Nome (ex.: OPENROUTER_API_KEY)
              </label>
              <input
                id={`${formId}-name`}
                className="input settings-form__input"
                value={keyName}
                onChange={(e) => setKeyName(e.target.value)}
                autoComplete="off"
                spellCheck={false}
                aria-invalid={keyError ? true : undefined}
                aria-describedby={keyError ? `${formId}-name-error` : undefined}
              />
              {keyError && (
                <span id={`${formId}-name-error`} className="field__error" role="alert">
                  {keyError.message}
                </span>
              )}
            </div>
            <div className="field">
              <label className="field__label" htmlFor={`${formId}-secret`}>
                Valor
              </label>
              <input
                id={`${formId}-secret`}
                className="input settings-form__input"
                type="password"
                value={secret}
                onChange={(e) => setSecret(e.target.value)}
                autoComplete="new-password"
                spellCheck={false}
                aria-invalid={valueError ? true : undefined}
                aria-describedby={valueError ? `${formId}-secret-error` : undefined}
              />
              {valueError && (
                <span id={`${formId}-secret-error`} className="field__error" role="alert">
                  {valueError.message}
                </span>
              )}
            </div>
            <div className="row-end">
              <button type="submit" className="btn btn--primary" disabled={busy || !isTauri}>
                {busy ? "Gravando…" : "Gravar no cofre"}
              </button>
            </div>
          </form>
        </>
      )}

      {generalError && (
        <div className="callout callout--error" role="alert">
          <p className="callout__body callout__body--mono">{generalError.message}</p>
        </div>
      )}
      {notice && (
        <p className="settings-form__notice" role="status">
          {notice}
        </p>
      )}
    </>
  );
};
