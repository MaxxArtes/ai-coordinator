/**
 * Form for a settings category backed by the Core IPC (Marco 4D.3, Etapa 4).
 * States follow decision D4 and the Etapa 4 authorization: saved, validated, applied, planned, unsupported.
 * "Aplicado" is shown only for preferences the interface really applies; persisted-only values say so.
 */
import React, { useEffect, useId, useMemo, useState } from "react";
import { Icon } from "../ui/Icon";
import {
  APPLIED_BY_INTERFACE,
  updateSettings,
  type BackedCategory,
  type CallTauri,
  type SettingsSnapshot,
  type StructuredError,
} from "./api";
import { CATEGORY_FIELDS, fieldPath, type UiField } from "./fields";

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

function draftFrom(category: BackedCategory, values: Record<string, any> | undefined): Draft {
  const draft: Draft = {};
  for (const field of CATEGORY_FIELDS[category]) {
    const raw = field.parent ? values?.[field.parent]?.[field.key] : values?.[field.key];
    draft[field.key] = field.kind === "bool" ? Boolean(raw) : raw === null || raw === undefined ? "" : String(raw);
  }
  return draft;
}

function valuesFrom(category: BackedCategory, draft: Draft): Record<string, any> {
  const out: Record<string, any> = {};
  for (const field of CATEGORY_FIELDS[category]) {
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
  const [error, setError] = useState<StructuredError | null>(null);
  const [notice, setNotice] = useState<string>("");
  const [saving, setSaving] = useState(false);

  useEffect(() => {
    setDraft(draftFrom(category, current?.values));
  }, [category, current]);

  const saved = (current?.saved_fields.length ?? 0) > 0;
  const unsupported = current?.application === "unsupported";
  const appliedKeys = useMemo(
    () => fields.filter((f) => APPLIED_BY_INTERFACE.has(fieldPath(category, f))).map((f) => f.key),
    [category, fields]
  );

  const fieldError = (field: UiField) => (error?.field === fieldPath(category, field) ? error : null);
  const generalError = error && !fields.some((f) => error.field === fieldPath(category, f)) ? error : null;

  const handleSubmit = async (event: React.FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    setSaving(true);
    setError(null);
    setNotice("");
    try {
      const next = await updateSettings(callTauri, category, valuesFrom(category, draft));
      onSaved(next);
      const appliedLabels = fields.filter((f) => appliedKeys.includes(f.key)).map((f) => f.label.toLowerCase());
      setNotice(
        unsupported
          ? "Salvo e validado no backend. Não aplicado: o desktop ainda não executa missões."
          : appliedKeys.length === 0
            ? "Salvo e validado no backend. A aplicação desta categoria ainda está pendente."
            : appliedKeys.length === fields.length
              ? "Salvo e validado no backend. Aplicado na interface."
              : `Salvo e validado no backend. Aplicado na interface: ${appliedLabels.join(", ")}. Demais campos: aplicação pendente.`
      );
    } catch (err) {
      setError(err as StructuredError);
    } finally {
      setSaving(false);
    }
  };

  const fieldStatus = (field: UiField): string => {
    if (unsupported) return "Não suportado nesta versão: validado, mas não aplicado.";
    if (APPLIED_BY_INTERFACE.has(fieldPath(category, field))) return "Aplicado na interface.";
    return "Aplicação pendente: o valor é salvo e validado, mas ainda não tem efeito.";
  };

  return (
    <>
      <div className="settings__panel-header">
        <h3 className="settings__heading">{label}</h3>
        <div className="settings__badges">
          <span className={saved ? "badge badge--ok" : "badge badge--planned"}>{saved ? "Salvo" : "Padrão (não salvo)"}</span>
          {saved && <span className="badge badge--ok">Validado no backend</span>}
          {unsupported ? (
            <span className="badge badge--unsupported">Não suportado nesta versão</span>
          ) : appliedKeys.length === fields.length ? (
            <span className="badge badge--ok">Aplicado</span>
          ) : appliedKeys.length > 0 ? (
            <span className="badge badge--warn">Aplicação parcial</span>
          ) : (
            <span className="badge badge--warn">Aplicação pendente</span>
          )}
        </div>
      </div>
      <p className="settings__summary">{summary}</p>

      {loadError && (
        <div className="callout callout--error" role="alert">
          <p className="callout__title">
            <Icon name="alert" size="sm" />
            <span>Não foi possível carregar as configurações</span>
          </p>
          <p className="callout__body callout__body--mono">{loadError}</p>
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
                <fieldset className="settings-form__fieldset" aria-describedby={describedBy} aria-invalid={err ? true : undefined}>
                  <legend className="field__label">{field.label}</legend>
                  {field.options!.map((option) => (
                    <label key={option.value} className="checkbox-row">
                      <input
                        type="radio"
                        name={id}
                        value={option.value}
                        checked={draft[field.key] === option.value}
                        onChange={() => setDraft((d) => ({ ...d, [field.key]: option.value }))}
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
                    onChange={(e) => setDraft((d) => ({ ...d, [field.key]: e.target.checked }))}
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
                    aria-describedby={describedBy}
                    aria-invalid={err ? true : undefined}
                    onChange={(e) => setDraft((d) => ({ ...d, [field.key]: e.target.value }))}
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
        {notice && (
          <p className="settings-form__notice" role="status">
            {notice}
          </p>
        )}

        <div className="row-end">
          <button type="submit" className="btn btn--primary" disabled={saving || !isTauri || !snapshot}>
            {saving ? "Salvando…" : "Salvar"}
          </button>
        </div>
      </form>
    </>
  );
};
