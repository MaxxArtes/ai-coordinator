/**
 * Configurações → Provedores: credentials in the profile vault (Marco 4D.3, Etapa 4; decision D8).
 * - Only key names are ever listed; the secret value is sent once and the field is cleared right after.
 * - Without a secure store (anything but DPAPI on Windows today) no input is offered and nothing is saved.
 * - Stored credentials are not used by any provider integration yet: this is stated, never implied.
 * - Replacing an existing credential or removing one asks for an explicit confirmation first: the vault keeps
 *   no history, so the previous value cannot be recovered by the application afterwards. The question is
 *   decided on the key list read from the vault right before writing, and it is bound to the name and value
 *   that were submitted. Known limit: the vault has no "write only if absent" operation, so another writer
 *   can still create the key between that read and the write (closing this needs a backend change).
 * - Leaving the panel cancels whatever was not sent to the vault yet: a check in flight, an open confirmation.
 * - A failure is shown where it happened (a removal: in the list; a save: in or under the form), and what could
 *   not be read from the vault is reported apart from the outcome of a save or removal.
 */
import React, { useCallback, useEffect, useId, useRef, useState } from "react";
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

/**
 * A pointer click only answers a confirmation after this long: the destructive button can appear under the
 * pointer that opened the question, and the second click of a double click must not answer it unread.
 * Keyboard activation is not delayed (the focus starts on Cancelar).
 */
export const CONFIRM_ARM_DELAY_MS = 600;

/** Clock of the arm delay. Monotonic: a correction of the system time must not leave a question unanswerable. */
export const armingClock = { now: (): number => performance.now() };

/** A destructive vault operation waiting for the operator's explicit confirmation. */
type PendingConfirmation = { kind: "replace" | "delete"; name: string };

/** What the panel is doing with the vault right now; "checking" reads key names only and writes nothing. */
type BusyState = false | { kind: "checking" | "saving" | "deleting"; name: string };

/** A failed save or removal; only a failed save is mapped onto the fields of the save form. */
type Failure = { error: StructuredError; source: "save" | "remove"; name: string };

/** Outcome shown in the status line; `neutral` when nothing was changed in the vault. */
type Notice = { text: string; neutral?: boolean };

const CONFIRMATION_TEXT = {
  replace: {
    title: (name: string) => `Substituir a credencial ${name}?`,
    body: "Já existe uma credencial com esse nome no cofre. O valor atual será sobrescrito pelo novo e não poderá ser recuperado pelo aplicativo.",
    action: "Substituir credencial",
  },
  delete: {
    title: (name: string) => `Remover a credencial ${name}?`,
    body: "O valor será apagado do cofre deste computador e não poderá ser recuperado pelo aplicativo.",
    action: "Remover credencial",
  },
} as const;

interface VaultConfirmationProps {
  pending: PendingConfirmation;
  idBase: string;
  cancelRef: React.RefObject<HTMLButtonElement>;
  onCancel: () => void;
  onConfirm: (event: React.MouseEvent<HTMLButtonElement>) => void;
}

/** Inline confirmation inside the panel; it shows the credential name only, never a value. */
const VaultConfirmation: React.FC<VaultConfirmationProps> = ({ pending, idBase, cancelRef, onCancel, onConfirm }) => {
  const text = CONFIRMATION_TEXT[pending.kind];
  // Shows the arm delay: until it has passed, the destructive answer looks unavailable to the pointer.
  const [armed, setArmed] = useState(false);
  useEffect(() => {
    const timer = window.setTimeout(() => setArmed(true), CONFIRM_ARM_DELAY_MS);
    return () => window.clearTimeout(timer);
  }, []);
  return (
    <div
      className="callout callout--warn"
      role="alertdialog"
      aria-labelledby={`${idBase}-confirm-title`}
      aria-describedby={`${idBase}-confirm-body`}
    >
      <p id={`${idBase}-confirm-title`} className="callout__title">
        <Icon name="alert" size="sm" />
        <span>{text.title(pending.name)}</span>
      </p>
      <p id={`${idBase}-confirm-body`} className="callout__body">
        {text.body}
      </p>
      <div className="row-end">
        <button ref={cancelRef} type="button" className="btn btn--sm btn--ghost" onClick={onCancel}>
          Cancelar
        </button>
        <button
          type="button"
          className={armed ? "btn btn--sm btn--danger" : "btn btn--sm btn--danger btn--arming"}
          onClick={onConfirm}
        >
          {text.action}
        </button>
      </div>
    </div>
  );
};

/** Holding Enter down must not walk through the panel (submit, cancel, submit again…): only real presses count. */
const ignoreRepeatedEnter = (event: React.KeyboardEvent) => {
  if (event.key === "Enter" && event.repeat) event.preventDefault();
};

export const VaultPanel: React.FC<VaultPanelProps> = ({ category, active, isTauri, callTauri }) => {
  const formId = useId();
  const [status, setStatus] = useState<VaultStatus | null>(null);
  const [keys, setKeys] = useState<string[]>([]);
  const [keyName, setKeyName] = useState("");
  const [secret, setSecret] = useState("");
  const [failure, setFailure] = useState<Failure | null>(null);
  // Kept apart from `failure`: not being able to read the vault says nothing about a save or a removal.
  const [loadFailure, setLoadFailure] = useState<StructuredError | null>(null);
  const [notice, setNotice] = useState<Notice | null>(null);
  const [busy, setBusy] = useState<BusyState>(false);
  const [pending, setPendingState] = useState<PendingConfirmation | null>(null);

  // One vault operation at a time, even for activations that land before the next render updates the controls.
  const operationRef = useRef(false);
  // Same value as `pending`, readable synchronously by the guards (a state read would be one render late).
  const pendingRef = useRef<PendingConfirmation | null>(null);
  // Name and value exactly as submitted, kept only while the "replace?" question is open.
  const submittedRef = useRef<{ name: string; value: string } | null>(null);
  // Changes whenever the panel is left or unmounted: a check still in flight then asks and writes nothing.
  const generationRef = useRef(0);
  // Moment from which a pointer click may answer the open confirmation.
  const armedAtRef = useRef(0);
  // Moment of the last pointer click on the destructive answer of the open confirmation.
  const lastAnswerClickRef = useRef<number | null>(null);

  const nameInputRef = useRef<HTMLInputElement>(null);
  const secretInputRef = useRef<HTMLInputElement>(null);
  const keysHeadingRef = useRef<HTMLHeadingElement>(null);
  const submitRef = useRef<HTMLButtonElement>(null);
  const confirmCancelRef = useRef<HTMLButtonElement>(null);
  const failureRef = useRef<HTMLDivElement>(null);
  // Element to receive focus once it is rendered and enabled again (after a confirmation closes, the
  // buttons that triggered it come back from `disabled`; focusing them earlier would be a no-op).
  const focusNextRef = useRef<HTMLElement | null>(null);
  // Remover button that opened a delete confirmation, so Cancelar can give the focus back to it.
  const returnFocusRef = useRef<HTMLElement | null>(null);

  const setPending = (next: PendingConfirmation | null) => {
    pendingRef.current = next;
    setPendingState(next);
  };

  const openConfirmation = (next: PendingConfirmation) => {
    armedAtRef.current = armingClock.now() + CONFIRM_ARM_DELAY_MS;
    lastAnswerClickRef.current = null;
    setPending(next);
  };

  const refresh = useCallback(async () => {
    // Only the load state: the outcome of a save or removal is not wiped by coming back to the panel.
    setLoadFailure(null);
    try {
      const st = await getVaultStatus(callTauri);
      setStatus(st);
      setKeys(st.available ? await listVaultKeys(callTauri) : []);
    } catch (err) {
      setLoadFailure(err as StructuredError);
    }
  }, [callTauri]);

  useEffect(() => {
    if (active && isTauri) void refresh();
  }, [active, isTauri, refresh]);

  // Never keep a typed secret, nor a question or a check that would write it, around when the panel is left.
  // Outcomes already shown go too; one that arrives while the panel is hidden stays until it has been seen.
  useEffect(() => {
    if (active) return;
    generationRef.current += 1;
    submittedRef.current = null;
    setSecret("");
    setPending(null);
    setFailure(null);
    setNotice(null);
  }, [active]);

  useEffect(
    () => () => {
      generationRef.current += 1;
      submittedRef.current = null;
    },
    []
  );

  // The safe choice gets the focus when a confirmation opens.
  useEffect(() => {
    if (pending && active) confirmCancelRef.current?.focus();
  }, [pending, active]);

  // A removal question about a credential that is no longer listed (removed elsewhere, seen on a reload) has
  // no row to be shown in: it is withdrawn, instead of leaving the panel locked behind an invisible question.
  useEffect(() => {
    const current = pendingRef.current;
    if (!current || current.kind !== "delete" || keys.includes(current.name)) return;
    returnFocusRef.current = null;
    focusNextRef.current = keysHeadingRef.current;
    setPending(null);
    setNotice({ text: `A credencial ${current.name} não está mais no cofre.`, neutral: true });
  }, [keys, pending]);

  useEffect(() => {
    const el = focusNextRef.current as (HTMLElement & { disabled?: boolean }) | null;
    if (el && el.isConnected && !el.disabled) {
      el.focus();
      focusNextRef.current = null;
    }
  });

  // The panel scrolls when the list is long: a failure is brought into view (after the focus has moved).
  useEffect(() => {
    if (failure) failureRef.current?.scrollIntoView?.({ block: "nearest" });
  }, [failure]);

  // When a save ends, the focus moves on only if it is still where the save left it: an operator who has
  // meanwhile gone to another control (a category tab, the close button) keeps the focus there.
  const focusAfterSave = (el: HTMLElement | null) => {
    const current = document.activeElement;
    const parked = [null, document.body, submitRef.current, nameInputRef.current, secretInputRef.current];
    if (parked.includes(current as HTMLElement | null)) focusNextRef.current = el;
  };

  // Reloads the list after an operation that already succeeded. If the reload fails, the list shows what the
  // operation is known to have done instead of an error unrelated to its outcome.
  const reloadKeys = async (known: (shown: string[]) => string[]) => {
    try {
      setKeys(await listVaultKeys(callTauri));
    } catch {
      setKeys(known);
    }
  };

  const writeSecret = async (name: string, value: string, replacing: boolean) => {
    setBusy({ kind: "saving", name });
    setFailure(null);
    setNotice(null);
    // The confirmation that had the focus is about to unmount: park it on the submit button meanwhile.
    if (replacing) focusNextRef.current = submitRef.current;
    try {
      try {
        await setVaultSecret(callTauri, name, value);
      } catch (err) {
        const error = err as StructuredError;
        setFailure({ error, source: "save", name });
        // Keeps the focus inside Configurações: on the field the backend rejected, else on Gravar.
        focusAfterSave(
          error.field === "key" ? nameInputRef.current : error.field === "value" ? secretInputRef.current : submitRef.current
        );
        return;
      }
      setSecret("");
      setKeyName("");
      setNotice({
        text: replacing
          ? `Credencial ${name} substituída no cofre. O valor não é exibido.`
          : `Credencial ${name} gravada no cofre. O valor não é exibido.`,
      });
      focusAfterSave(nameInputRef.current);
      await reloadKeys((shown) => (shown.includes(name) ? shown : [...shown, name].sort()));
    } finally {
      setBusy(false);
    }
  };

  const removeSecret = async (name: string) => {
    setBusy({ kind: "deleting", name });
    setFailure(null);
    setNotice(null);
    // The Remover button of this credential is about to go away: park the focus on the list heading.
    focusNextRef.current = keysHeadingRef.current;
    try {
      let deleted: boolean;
      try {
        ({ deleted } = await deleteVaultSecret(callTauri, name));
      } catch (err) {
        setFailure({ error: err as StructuredError, source: "remove", name });
        return;
      }
      setNotice(
        deleted
          ? { text: `Credencial ${name} removida do cofre.` }
          : { text: `A credencial ${name} não estava mais no cofre; nada foi removido.`, neutral: true }
      );
      await reloadKeys((shown) => shown.filter((key) => key !== name));
    } finally {
      setBusy(false);
    }
  };

  const handleSave = async (event: React.FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    if (operationRef.current || pendingRef.current) return;
    const name = keyName.trim();
    const value = secret;
    // Nothing typed (for instance Enter again right after a save): nothing to send, nothing on screen changes.
    if (name === "" && value === "") return;
    if (value === "") {
      // Local mirror of the backend rule, so an existing name never gets a "replace?" question for no value.
      setNotice(null);
      setFailure({
        error: { code: -32602, message: "Informe o valor da credencial.", field: "value", reason: "invalid_value" },
        source: "save",
        name,
      });
      focusNextRef.current = secretInputRef.current;
      return;
    }
    operationRef.current = true;
    const generation = generationRef.current;
    try {
      setBusy({ kind: "checking", name });
      setFailure(null);
      setNotice(null);
      // Decide on a fresh list: the one on screen may be stale if the vault changed since it was loaded.
      let current: string[] | null = null;
      let checkError: StructuredError | null = null;
      try {
        current = await listVaultKeys(callTauri);
      } catch (err) {
        checkError = err as StructuredError;
      }
      setBusy(false);
      // The panel was left while the check was in flight: nothing was sent, so nothing is asked or written.
      if (generation !== generationRef.current) {
        setNotice({ text: "Envio cancelado ao sair do painel: nada foi gravado no cofre.", neutral: true });
        return;
      }
      // Nothing is written when the list cannot be read.
      if (!current) {
        setFailure({ error: checkError as StructuredError, source: "save", name });
        focusAfterSave(submitRef.current);
        return;
      }
      setKeys(current);
      if (current.includes(name)) {
        submittedRef.current = { name, value };
        openConfirmation({ kind: "replace", name });
        return;
      }
      await writeSecret(name, value, false);
    } finally {
      operationRef.current = false;
    }
  };

  const requestDelete = (name: string, trigger: HTMLElement) => {
    if (operationRef.current || pendingRef.current) return;
    setFailure(null);
    setNotice(null);
    focusNextRef.current = null;
    returnFocusRef.current = trigger;
    openConfirmation({ kind: "delete", name });
  };

  // Cancelar keeps the typed name and value in the form, so the operator can change the name without retyping
  // the credential; they are discarded on success, on leaving the panel and on closing Configurações.
  const cancelConfirmation = () => {
    const current = pendingRef.current;
    if (!current || operationRef.current) return;
    focusNextRef.current = current.kind === "delete" ? returnFocusRef.current : submitRef.current;
    returnFocusRef.current = null;
    submittedRef.current = null;
    setPending(null);
  };

  const confirmPending = async () => {
    const current = pendingRef.current;
    if (!current || operationRef.current) return;
    operationRef.current = true;
    const submitted = submittedRef.current;
    submittedRef.current = null;
    returnFocusRef.current = null;
    setPending(null);
    try {
      if (current.kind === "delete") await removeSecret(current.name);
      else if (submitted && submitted.name === current.name) await writeSecret(submitted.name, submitted.value, true);
    } finally {
      operationRef.current = false;
    }
  };

  const handleConfirmClick = (event: React.MouseEvent<HTMLButtonElement>) => {
    // Keyboard activation (detail 0) is not delayed. A pointer click must not answer the question as part of
    // the gesture that opened it (see CONFIRM_ARM_DELAY_MS): not before the question is armed, and not as the
    // next click of a multi-click that started before that. Insisting on the armed button does get through.
    if (event.detail > 0) {
      const now = armingClock.now();
      const previous = lastAnswerClickRef.current;
      lastAnswerClickRef.current = now;
      if (now < armedAtRef.current) return;
      if (event.detail > 1 && (previous === null || previous < armedAtRef.current)) return;
    }
    void confirmPending();
  };

  // While a question is open, Escape answers it ("Cancelar") from anywhere in Configurações, instead of closing
  // the whole dialog and losing what was typed. Capture phase: it runs before the dialog's own Escape handling.
  const cancelRef = useRef(cancelConfirmation);
  cancelRef.current = cancelConfirmation;
  useEffect(() => {
    if (!pending) return;
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key !== "Escape") return;
      event.preventDefault();
      event.stopPropagation();
      cancelRef.current();
    };
    document.addEventListener("keydown", onKeyDown, true);
    return () => document.removeEventListener("keydown", onKeyDown, true);
  }, [pending]);

  const working = busy !== false;
  const locked = working || pending !== null;
  const progress = !busy
    ? ""
    : busy.kind === "checking"
      ? "Conferindo o cofre…"
      : busy.kind === "saving"
        ? `Gravando a credencial ${busy.name}…`
        : `Removendo a credencial ${busy.name}…`;

  const unavailable = status !== null && !status.available;
  const saveError = failure?.source === "save" ? failure.error : null;
  const keyError = saveError?.field === "key" ? saveError : null;
  const valueError = saveError?.field === "value" ? saveError : null;
  const removeFailure = failure?.source === "remove" ? failure : null;
  const saveFailure = failure?.source === "save" && !keyError && !valueError ? failure : null;

  const confirmation = pending && (
    <VaultConfirmation
      pending={pending}
      idBase={formId}
      cancelRef={confirmCancelRef}
      onCancel={cancelConfirmation}
      onConfirm={handleConfirmClick}
    />
  );

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

      {loadFailure && (
        <div className="callout callout--error" role="alert">
          <p className="callout__title">
            <span>Não foi possível ler o cofre.</span>
          </p>
          <p className="callout__body callout__body--mono">{loadFailure.message}</p>
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
          <section aria-labelledby={`${formId}-keys`} onKeyDown={ignoreRepeatedEnter}>
            <h4 id={`${formId}-keys`} ref={keysHeadingRef} className="settings__section-title" tabIndex={-1}>
              Credenciais no cofre
            </h4>
            {/* Next to the list it is about: the end of the panel can be out of view when the list is long. */}
            {removeFailure && (
              <div ref={failureRef} className="callout callout--error vault-keys__failure" role="alert">
                <p className="callout__title">
                  <span>Não foi possível remover a credencial {removeFailure.name}.</span>
                </p>
                <p className="callout__body callout__body--mono">{removeFailure.error.message}</p>
              </div>
            )}
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
                      onClick={(e) => requestDelete(name, e.currentTarget)}
                      disabled={locked}
                      aria-label={`Remover a credencial ${name}`}
                    >
                      {busy && busy.kind === "deleting" && busy.name === name ? "Removendo…" : "Remover"}
                    </button>
                    {/* The question stays with the credential it is about, which remains visible above it. */}
                    {pending?.kind === "delete" && pending.name === name && confirmation}
                  </li>
                ))}
              </ul>
            )}
          </section>

          <form
            className="settings-form"
            onSubmit={handleSave}
            onKeyDown={ignoreRepeatedEnter}
            noValidate
            aria-labelledby={`${formId}-new`}
          >
            <h4 id={`${formId}-new`} className="settings__section-title">
              Gravar credencial
            </h4>
            <div className="field">
              <label className="field__label" htmlFor={`${formId}-name`}>
                Nome (ex.: OPENROUTER_API_KEY)
              </label>
              <input
                id={`${formId}-name`}
                ref={nameInputRef}
                className="input settings-form__input"
                value={keyName}
                onChange={(e) => setKeyName(e.target.value)}
                readOnly={working}
                disabled={pending !== null}
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
                ref={secretInputRef}
                className="input settings-form__input"
                type="password"
                value={secret}
                onChange={(e) => setSecret(e.target.value)}
                readOnly={working}
                disabled={pending !== null}
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
              {/* While working the button is only aria-disabled: a real `disabled` would drop the focus to the
                  document, out of reach of the dialog's keyboard handling. Repeated submits are refused by the guard. */}
              <button
                ref={submitRef}
                type="submit"
                className="btn btn--primary"
                disabled={pending !== null || !isTauri}
                aria-disabled={working || undefined}
              >
                {busy && busy.kind === "saving" ? "Gravando…" : busy && busy.kind === "checking" ? "Conferindo o cofre…" : "Gravar no cofre"}
              </button>
            </div>
            {/* Below the submit button, so the destructive answer does not take the place of the button just clicked. */}
            {pending?.kind === "replace" && confirmation}
          </form>

          {saveFailure && (
            <div ref={failureRef} className="callout callout--error" role="alert">
              <p className="callout__body callout__body--mono">{saveFailure.error.message}</p>
            </div>
          )}
          {/* Mounted for as long as the vault is usable, so progress and outcome are announced as changes of
              one live region. */}
          <p
            className={progress || notice?.neutral ? "settings-form__notice settings-form__notice--neutral" : "settings-form__notice"}
            role="status"
          >
            {progress || notice?.text}
          </p>
        </>
      )}
    </>
  );
};
