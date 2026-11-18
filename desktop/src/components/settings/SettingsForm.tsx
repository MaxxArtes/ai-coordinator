/**
 * Form for a settings category backed by the Core IPC (Marco 4D.3, Etapas 4 e 7).
 * States follow decision D4 and the Etapa 4 authorization: saved, validated, applied, planned, unsupported.
 * "Aplicado" is shown only when it is true: the snapshot was loaded, the category is validated and writable, and the
 * interface really applies the preference. A preference used only at the next start says so.
 * Stored values that are invalid are never shown: the field shows the default and the stored value is kept until
 * this field is changed and saved. Settings saved by another version are protected: the form is read-only.
 */
import React, { useEffect, useId, useMemo, useState } from "react";
import { Icon } from "../ui/Icon";
import {
  APPLIED_BY_INTERFACE,
  APPLIED_ON_NEXT_START,
  categoryInEffect,
  invalidFieldsOf,
  updateSettings,
  type BackedCategory,
  type CallTauri,
  type SettingsSnapshot,
  type StructuredError,
} from "./api";
import { CATEGORY_FIELDS, fieldPath, type UiField } from "./fields";
import { SHORTCUT_LABELS } from "../shell/shortcuts";

export interface SettingsFormProps {
  category: BackedCategory;
  label: string;
  summary: string;
  snapshot: SettingsSnapshot | null;
  loadError: string | null;
  isTauri: boolean;
  callTauri: CallTauri;
  onSaved: (snapshot: SettingsSnapshot) => void;
}

type Draft = Record<string, string | boolean>;

const READ_ONLY_TEXT = "As configurações foram gravadas por outra versão e estão protegidas. Nada foi alterado.";
const BACKED_CATEGORIES = Object.keys(CATEGORY_FIELDS) as BackedCategory[];

function draftFrom(category: BackedCategory, values: Record<string, any> | undefined): Draft {
  const draft: Draft = {};
  for (const field of CATEGORY_FIELDS[category]) {
    const raw = field.parent ? values?.[field.parent]?.[field.key] : values?.[field.key];
    draft[field.key] = field.kind === "bool" ? Boolean(raw) : raw === null || raw === undefined ? "" : String(raw);
  }
  return draft;
}

/** Values to send. A field whose stored value is invalid goes only once it was changed: until then it stays stored. */
function valuesFrom(category: BackedCategory, draft: Draft, keep: (field: UiField) => boolean): Record<string, any> {
  const out: Record<string, any> = {};
  for (const field of CATEGORY_FIELDS[category]) {
    if (!keep(field)) continue;
    let value: any = draft[field.key];
    if (field.kind === "int") {
      const n = Number(value);
      value = value !== "" && Number.isInteger(n) ? n : value; // non-numbers go as-is: the backend rejects them
    }
    if (field.parent) out[field.parent] = { ...(out[field.parent] ?? {}), [field.key]: value };
    else out[field.key] = value;
  }
  return out;
}

/** The host may name an invalid field by its key, its parent object or its full path. */
function isInvalid(invalid: string[], category: BackedCategory, field: UiField): boolean {
  const names = [field.key, `${category}.${field.key}`, fieldPath(category, field)];
  if (field.parent) names.push(field.parent, `${category}.${field.parent}`, `${field.parent}.${field.key}`);
  return names.some((name) => invalid.includes(name));
}

export const SettingsForm: React.FC<SettingsFormProps> = ({
  category,
  label,
  summary,
  snapshot,
  loadError,
  isTauri,
  callTauri,
  onSaved,
}) => {
  const formId = useId();
  const current = snapshot?.categories?.[category];
  const fields = CATEGORY_FIELDS[category];
  const [draft, setDraft] = useState<Draft>(() => draftFrom(category, current?.values));
  // Fields the operator changed since the last load: only these replace an invalid stored value.
  const [touched, setTouched] = useState<ReadonlySet<string>>(new Set());
  const [error, setError] = useState<StructuredError | null>(null);
  const [notice, setNotice] = useState<{ text: string; neutral?: boolean } | null>(null);
  const [saving, setSaving] = useState(false);

  useEffect(() => {
    setDraft(draftFrom(category, current?.values));
    setTouched(new Set());
  }, [category, current]);

  const loaded = isTauri && !!snapshot && !loadError;
  const readOnly = !!snapshot?.read_only;
  const invalid = invalidFieldsOf(snapshot, category);
  const invalidField = (field: UiField) => isInvalid(invalid, category, field);
  const invalidCount = fields.filter(invalidField).length;
  const anyInvalid = BACKED_CATEGORIES.some((c) => invalidFieldsOf(snapshot, c).length > 0);
  const inEffect = loaded && categoryInEffect(snapshot, category);
  const saved = (current?.saved_fields.length ?? 0) > 0;
  const unsupported = current?.application === "unsupported";
  const appliedKeys = useMemo(
    () => fields.filter((f) => APPLIED_BY_INTERFACE.has(fieldPath(category, f))).map((f) => f.key),
    [category, fields]
  );
  const nextStartOnly = fields.every((f) => APPLIED_ON_NEXT_START.has(fieldPath(category, f)));

  const fieldError = (field: UiField) => (error?.field === fieldPath(category, field) ? error : null);
  const generalError = error && !fields.some((f) => error.field === fieldPath(category, f)) ? error : null;

  const change = (field: UiField, value: string | boolean) => {
    if (readOnly) return;
    setDraft((d) => ({ ...d, [field.key]: value }));
    setTouched((t) => new Set(t).add(field.key));
  };

  const handleSubmit = async (event: React.FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    if (readOnly || saving) return;
    // A field sent with its default would silently replace an invalid stored value the operator never changed.
    const groupTouched = (field: UiField) => fields.some((f) => f.parent === field.parent && touched.has(f.key));
    const keep = (field: UiField) => !invalidField(field) || touched.has(field.key) || (!!field.parent && groupTouched(field));
    const values = valuesFrom(category, draft, keep);
    setError(null);
    if (Object.keys(values).length === 0) {
      setNotice({ text: "Nada foi enviado: o valor salvo inválido só é substituído depois que você altera o campo.", neutral: true });
      return;
    }
    setSaving(true);
    setNotice(null);
    try {
      const next = await updateSettings(callTauri, category, values);
      onSaved(next);
      const stillInvalid = invalidFieldsOf(next, category).length > 0;
      const appliedLabels = fields.filter((f) => appliedKeys.includes(f.key)).map((f) => f.label.toLowerCase());
      setNotice({ text:
        unsupported
          ? "Salvo e validado no backend. Não aplicado: o desktop ainda não executa missões."
          : stillInvalid
            ? "Salvo e validado no backend. Campos com valor salvo inválido continuam preservados e usando o padrão."
            : nextStartOnly
              ? "Salvo e validado no backend. Vale a partir da próxima inicialização."
              : appliedKeys.length === 0
                ? "Salvo e validado no backend. A aplicação desta categoria ainda está pendente."
                : appliedKeys.length === fields.length
                  ? "Salvo e validado no backend. Aplicado na interface."
                  : `Salvo e validado no backend. Aplicado na interface: ${appliedLabels.join(", ")}. Demais campos: aplicação pendente.`,
      });
    } catch (err) {
      setError(err as StructuredError);
    } finally {
      setSaving(false);
    }
  };

  const fieldStatus = (field: UiField): string => {
    if (unsupported) return "Não suportado nesta versão: validado, mas não aplicado.";
    if (readOnly) return "Somente leitura: configurações de outra versão, protegidas.";
    if (!loaded) return "Não aplicado: as configurações não foram carregadas.";
    if (invalidField(field)) return "Valor salvo inválido: usando o padrão. O valor salvo fica preservado até você alterar este campo e salvar.";
    if (APPLIED_ON_NEXT_START.has(fieldPath(category, field))) return "Vale a partir da próxima inicialização.";
    if (APPLIED_BY_INTERFACE.has(fieldPath(category, field))) return "Aplicado na interface.";
    return "Aplicação pendente: o valor é salvo e validado, mas ainda não tem efeito.";
  };

  const applicationBadge = unsupported ? (
    <span className="badge badge--unsupported">Não suportado nesta versão</span>
  ) : readOnly ? (
    <span className="badge badge--warn">Protegido: somente leitura</span>
  ) : !loaded ? (
    <span className="badge badge--warn">Não aplicado: configurações não carregadas</span>
  ) : !inEffect ? (
    <span className="badge badge--warn">Valores inválidos: usando o padrão</span>
  ) : nextStartOnly ? (
    <span className="badge badge--planned">Aplicado na próxima inicialização</span>
  ) : appliedKeys.length === fields.length ? (
    <span className="badge badge--ok">Aplicado</span>
  ) : appliedKeys.length > 0 ? (
    <span className="badge badge--warn">Aplicação parcial</span>
  ) : (
    <span className="badge badge--warn">Aplicação pendente</span>
  );

  return (
    <>
      <div className="settings__panel-header">
        <h3 className="settings__heading">{label}</h3>
        <div className="settings__badges">
          <span className={saved ? "badge badge--ok" : "badge badge--planned"}>{saved ? "Salvo" : "Padrão (não salvo)"}</span>
          {saved && current?.validated && <span className="badge badge--ok">Validado no backend</span>}
          {applicationBadge}
        </div>
      </div>
      <p className="settings__summary">{summary}</p>
      {category === "geral" && <>
        <h4>Atalhos de teclado</h4>
        <dl>{SHORTCUT_LABELS.map(([keys, action]) => <React.Fragment key={keys}><dt><kbd>{keys}</kbd></dt><dd>{action}</dd></React.Fragment>)}</dl>
        <p>O histórico pode ser exportado; restaurar sessões ainda não é suportado.</p>
      </>}

      {loadError && (
        <div className="callout callout--error" role="alert">
          <p className="callout__title">
            <Icon name="alert" size="sm" />
            <span>Não foi possível carregar as configurações</span>
          </p>
          <p className="callout__body callout__body--mono">{loadError}</p>
          <p className="callout__body">A interface usa os padrões. Nada foi sobrescrito.</p>
        </div>
      )}
      {readOnly && (
        <div className="callout callout--warn" role="status">
          <p className="callout__title">
            <Icon name="shield" size="sm" />
            <span>Configurações protegidas</span>
          </p>
          <p className="callout__body">{snapshot?.stored_issue?.message || READ_ONLY_TEXT}</p>
          <p className="callout__body">
            Os campos ficam somente leitura: esta versão não grava sobre configurações de outra versão. Os valores
            mostrados são os que esta versão está usando.
          </p>
        </div>
      )}
      {/* The host's stored_issue covers every category: each form speaks only of its own invalid fields. */}
      {!readOnly && (invalidCount > 0 || (snapshot?.stored_issue && !anyInvalid)) && (
        <div className="callout callout--warn" role="status">
          <p className="callout__title">
            <Icon name="alert" size="sm" />
            <span>
              {invalidCount === 1
                ? "1 campo com valor salvo inválido"
                : invalidCount > 1
                  ? `${invalidCount} campos com valor salvo inválido`
                  : "Configurações salvas com problema"}
            </span>
          </p>
          {invalidCount > 0 ? (
            <p className="callout__body">
              O campo mostra o padrão, que é o que está em uso. O valor salvo não é exibido e fica preservado até você
              alterar o campo e salvar.
            </p>
          ) : (
            <p className="callout__body">{snapshot?.stored_issue?.message}</p>
          )}
        </div>
      )}
      {!readOnly && snapshot?.migration?.required && (
        <div className="callout callout--info">
          <p className="callout__body">
            Configurações salvas em formato antigo: a primeira gravação as converte para a versão{" "}
            {snapshot.migration.to_version}, depois de guardar uma cópia do perfil (profile.json.pre-v1.bak).
          </p>
        </div>
      )}
      {!isTauri && (
        <div className="callout callout--info">
          <p className="callout__body">Modo preview: as alterações não são salvas.</p>
        </div>
      )}

      <form className="settings-form" onSubmit={handleSubmit} aria-labelledby={`${formId}-legend`} noValidate>
        <span id={`${formId}-legend`} className="sr-only">
          {label}
        </span>
        {fields.map((field) => {
          const id = `${formId}-${field.key}`;
          const err = fieldError(field);
          const describedBy = [`${id}-status`, field.hint ? `${id}-hint` : "", err ? `${id}-error` : ""].filter(Boolean).join(" ");
          return (
            <div key={field.key} className="settings-form__row">
              {field.kind === "enum" ? (
                <fieldset
                  className="settings-form__fieldset"
                  role="radiogroup"
                  aria-describedby={describedBy}
                  aria-invalid={err ? true : undefined}
                  aria-readonly={readOnly || undefined}
                >
                  <legend className="field__label">{field.label}</legend>
                  {field.options!.map((option) => (
                    <label key={option.value} className="checkbox-row">
                      <input
                        type="radio"
                        name={id}
                        value={option.value}
                        checked={draft[field.key] === option.value}
                        onChange={() => change(field, option.value)}
                      />
                      <span>{option.label}</span>
                    </label>
                  ))}
                </fieldset>
              ) : field.kind === "bool" ? (
                <label className="checkbox-row">
                  <input
                    id={id}
                    type="checkbox"
                    checked={Boolean(draft[field.key])}
                    aria-describedby={describedBy}
                    aria-invalid={err ? true : undefined}
                    aria-readonly={readOnly || undefined}
                    onChange={(e) => change(field, e.target.checked)}
                  />
                  <span>{field.label}</span>
                </label>
              ) : (
                <div className="field">
                  <label className="field__label" htmlFor={id}>
                    {field.label}
                  </label>
                  <input
                    id={id}
                    className="input settings-form__input"
                    type={field.kind === "int" ? "number" : "text"}
                    min={field.min}
                    max={field.max}
                    maxLength={field.kind === "text" ? field.max : undefined}
                    value={String(draft[field.key] ?? "")}
                    readOnly={readOnly}
                    aria-describedby={describedBy}
                    aria-invalid={err ? true : undefined}
                    onChange={(e) => change(field, e.target.value)}
                  />
                </div>
              )}
              {field.hint && (
                <span id={`${id}-hint`} className="field__hint">
                  {field.hint}
                </span>
              )}
              <span id={`${id}-status`} className="settings-form__status">
                {fieldStatus(field)}
              </span>
              {err && (
                <span id={`${id}-error`} className="field__error" role="alert">
                  {err.message}
                </span>
              )}
            </div>
          );
        })}

        {generalError && (
          <div className="callout callout--error" role="alert">
            <p className="callout__body callout__body--mono">{generalError.message}</p>
          </div>
        )}
        <p className={notice?.neutral ? "settings-form__notice settings-form__notice--neutral" : "settings-form__notice"} role="status">
          {notice?.text}
        </p>

        <div className="row-end">
          <button type="submit" className="btn btn--primary" disabled={saving || !isTauri || !snapshot || readOnly}>
            {saving ? "Salvando…" : "Salvar"}
          </button>
        </div>
      </form>
    </>
  );
};
