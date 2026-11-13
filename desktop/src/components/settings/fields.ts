import type { BackedCategory } from "./api";

/** UI description of the backed settings (labels and input hints). The Core IPC schema remains authoritative. */
export type FieldKind = "text" | "bool" | "enum" | "int";

export interface UiField {
  key: string;
  label: string;
  kind: FieldKind;
  hint?: string;
  min?: number;
  max?: number;
  options?: { value: string; label: string }[];
  /** For nested objects (coordinator.flight_budget): path inside the category value. */
  parent?: string;
}

export const CATEGORY_FIELDS: Record<BackedCategory, UiField[]> = {
  perfil: [{ key: "display_name", label: "Nome de exibição", kind: "text", max: 64, hint: "De 1 a 64 caracteres." }],
  geral: [
    {
      key: "open_last_project_on_start",
      label: "Abrir o último projeto autorizado ao iniciar",
      kind: "bool",
    },
  ],
  aparencia: [
    {
      key: "stimulation",
      label: "Estimulação visual",
      kind: "enum",
      options: [
        { value: "standard", label: "Padrão" },
        { value: "low", label: "Baixa estimulação (contraste suave, sem animações)" },
      ],
    },
    {
      key: "density",
      label: "Densidade",
      kind: "enum",
      options: [
        { value: "comfortable", label: "Confortável" },
        { value: "compact", label: "Compacta" },
      ],
    },
  ],
  terminal: [
    { key: "font_size", label: "Tamanho da fonte", kind: "int", min: 10, max: 24 },
    { key: "cursor_blink", label: "Cursor piscante", kind: "bool" },
    { key: "scrollback_lines", label: "Linhas de histórico", kind: "int", min: 100, max: 1000, hint: "Limite do buffer: 1.000 linhas." },
  ],
  coordinator: [
    { key: "max_steps", parent: "flight_budget", label: "Passos máximos por missão", kind: "int", min: 1, max: 1000 },
    { key: "max_elapsed_seconds", parent: "flight_budget", label: "Duração máxima (segundos)", kind: "int", min: 60, max: 86400 },
    { key: "max_uncheckpointed_steps", parent: "flight_budget", label: "Passos sem checkpoint", kind: "int", min: 1, max: 100 },
    { key: "max_worker_calls", parent: "flight_budget", label: "Chamadas a workers", kind: "int", min: 1, max: 200 },
  ],
};

export const fieldPath = (category: BackedCategory, field: UiField) =>
  field.parent ? `${category}.${field.parent}.${field.key}` : `${category}.${field.key}`;
