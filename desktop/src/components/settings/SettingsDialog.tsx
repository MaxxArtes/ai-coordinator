/**
 * Unified settings dialog (Marco 4D.3.1): side navigation over the nine roadmap categories.
 *
 * Keyboard (WAI-ARIA tabs, vertical): ArrowUp/ArrowDown move between categories, Home/End jump to the
 * first/last; only the selected tab is in the Tab order. All panels stay mounted (inactive ones are hidden)
 * so an update in progress is never unmounted by switching categories.
 */
import React, { useCallback, useEffect, useRef, useState } from "react";
import { Dialog } from "../ui/Dialog";
import type { ConnectionStatus, RuntimeStatus } from "../shell/AppHeader";
import { AboutPanel } from "./AboutPanel";
import { CategoryPanel } from "./CategoryPanel";
import { SettingsForm } from "./SettingsForm";
import { VaultPanel } from "./VaultPanel";
import { getSettings, type BackedCategory, type SettingsSnapshot, type StructuredError } from "./api";
import { SETTINGS_CATEGORIES, type SettingsCategoryId } from "./categories";

export interface SettingsDialogProps {
  open: boolean;
  onClose: () => void;
  initialCategory?: SettingsCategoryId;
  isTauri: boolean;
  runtimeStatus: RuntimeStatus | null;
  connectionStatus: ConnectionStatus;
  activePanesCount: number;
  callTauri: (cmd: string, args?: Record<string, any>) => Promise<any>;
  /** Called with every settings snapshot loaded or saved, so the app applies interface-level preferences. */
  onSettingsChanged?: (snapshot: SettingsSnapshot) => void;
}

export const SettingsDialog: React.FC<SettingsDialogProps> = ({ open, ...rest }) =>
  open ? <SettingsDialogContent {...rest} /> : null;

const SettingsDialogContent: React.FC<Omit<SettingsDialogProps, "open">> = ({
  onClose,
  initialCategory = "perfil",
  isTauri,
  runtimeStatus,
  connectionStatus,
  activePanesCount,
  callTauri,
  onSettingsChanged,
}) => {
  const [snapshot, setSnapshot] = useState<SettingsSnapshot | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
  const onSettingsChangedRef = useRef(onSettingsChanged);
  onSettingsChangedRef.current = onSettingsChanged;

  const acceptSnapshot = useCallback((next: SettingsSnapshot) => {
    setSnapshot(next);
    setLoadError(null);
    onSettingsChangedRef.current?.(next);
  }, []);

  useEffect(() => {
    if (!isTauri) return;
    let alive = true;
    getSettings(callTauri)
      .then((next) => alive && acceptSnapshot(next))
      .catch((err: StructuredError) => alive && setLoadError(err.message));
    return () => {
      alive = false;
    };
  }, [isTauri, callTauri, acceptSnapshot]);

  const [active, setActive] = useState<SettingsCategoryId>(initialCategory);
  const [updatesLocked, setUpdatesLocked] = useState<boolean>(false);
  const tabRefs = useRef<Partial<Record<SettingsCategoryId, HTMLButtonElement | null>>>({});
  const initialTabRef = useRef<HTMLButtonElement | null>(null);

  const select = useCallback((id: SettingsCategoryId) => {
    setActive(id);
    tabRefs.current[id]?.focus();
  }, []);

  const handleTabKeyDown = useCallback(
    (event: React.KeyboardEvent<HTMLButtonElement>, index: number) => {
      const last = SETTINGS_CATEGORIES.length - 1;
      let next: number | null = null;
      if (event.key === "ArrowDown") next = index === last ? 0 : index + 1;
      else if (event.key === "ArrowUp") next = index === 0 ? last : index - 1;
      else if (event.key === "Home") next = 0;
      else if (event.key === "End") next = last;
      if (next === null) return;
      event.preventDefault();
      select(SETTINGS_CATEGORIES[next].id);
    },
    [select]
  );

  return (
    <Dialog
      open
      title="Configurações"
      size="lg"
      onClose={onClose}
      dismissible={!updatesLocked}
      closeLabel="Fechar configurações"
      initialFocusRef={initialTabRef}
    >
      <div className="settings">
        <nav className="settings__nav" aria-label="Categorias de configuração">
          <div role="tablist" aria-orientation="vertical" aria-label="Categorias" className="settings__tablist">
            {SETTINGS_CATEGORIES.map((category, index) => {
              const selected = category.id === active;
              return (
                <button
                  key={category.id}
                  ref={(el) => {
                    tabRefs.current[category.id] = el;
                    if (category.id === initialCategory) initialTabRef.current = el;
                  }}
                  type="button"
                  role="tab"
                  id={`settings-tab-${category.id}`}
                  aria-selected={selected}
                  aria-controls={`settings-panel-${category.id}`}
                  tabIndex={selected ? 0 : -1}
                  className="settings__tab"
                  onClick={() => select(category.id)}
                  onKeyDown={(event) => handleTabKeyDown(event, index)}
                >
                  <span>{category.label}</span>
                  {category.state === "planned" && <span className="settings__tab-state">planejado</span>}
                </button>
              );
            })}
          </div>
        </nav>

        {SETTINGS_CATEGORIES.map((category) => (
          <div
            key={category.id}
            role="tabpanel"
            id={`settings-panel-${category.id}`}
            aria-labelledby={`settings-tab-${category.id}`}
            hidden={category.id !== active}
            tabIndex={0}
            className="settings__panel"
          >
            {category.id === "sobre" ? (
              <AboutPanel
                active={active === "sobre"}
                isTauri={isTauri}
                runtimeStatus={runtimeStatus}
                connectionStatus={connectionStatus}
                activePanesCount={activePanesCount}
                callTauri={callTauri}
                onRequestClose={onClose}
                onUpdatesLockChange={setUpdatesLocked}
              />
            ) : category.backend === "settings" ? (
              <SettingsForm
                category={category.id as BackedCategory}
                label={category.label}
                summary={category.summary}
                snapshot={snapshot}
                loadError={loadError}
                isTauri={isTauri}
                callTauri={callTauri}
                onSaved={acceptSnapshot}
              />
            ) : category.backend === "vault" ? (
              <VaultPanel category={category} active={active === category.id} isTauri={isTauri} callTauri={callTauri} />
            ) : (
              <CategoryPanel category={category} />
            )}
          </div>
        ))}
      </div>
    </Dialog>
  );
};
