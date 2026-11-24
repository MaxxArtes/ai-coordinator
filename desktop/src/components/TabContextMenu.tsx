import React, { useEffect, useRef } from "react";
import { Icon } from "./ui/Icon";

export interface TabContextMenuProps {
  open: boolean;
  x: number;
  y: number;
  currentProjectPath: string;
  authorizedProjects: readonly string[];
  atLimit: boolean;
  busy?: boolean;
  onSelectProject: (path: string) => void;
  onOpenOtherProject?: () => void;
  onClose: () => void;
  /**
   * Where the focus goes on close when nothing focusable had it before the menu opened (a right-click on the strip
   * label leaves it on the page body): never left on the body.
   */
  onRestoreFocus?: () => void;
}

const folderLabel = (path: string): string => path.split(/[\\/]+/).filter(Boolean).pop() ?? path;

const normalizeWin = (p: string): string => p.replace(/[\\/]+$/, "").toLowerCase();

export const TabContextMenu: React.FC<TabContextMenuProps> = ({
  open,
  x,
  y,
  currentProjectPath,
  authorizedProjects,
  atLimit,
  busy = false,
  onSelectProject,
  onOpenOtherProject,
  onClose,
  onRestoreFocus,
}) => {
  const menuRef = useRef<HTMLDivElement>(null);
  const previousFocusRef = useRef<HTMLElement | null>(null);
  const restoreFocusRef = useRef(onRestoreFocus);
  restoreFocusRef.current = onRestoreFocus;

  useEffect(() => {
    if (!open) return;
    previousFocusRef.current = document.activeElement as HTMLElement | null;
    const items = menuRef.current?.querySelectorAll<HTMLButtonElement>('[role="menuitem"]:not([aria-disabled="true"])');
    items?.[0]?.focus();
    return () => {
      const previous = previousFocusRef.current;
      if (previous && previous !== document.body && document.contains(previous)) previous.focus();
      else restoreFocusRef.current?.();
    };
  }, [open]);

  useEffect(() => {
    if (!open) return;
    const onPointerDown = (event: MouseEvent) => {
      if (menuRef.current && !menuRef.current.contains(event.target as Node)) {
        onClose();
      }
    };
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") {
        event.preventDefault();
        event.stopPropagation();
        onClose();
      }
    };
    window.addEventListener("mousedown", onPointerDown);
    window.addEventListener("keydown", onKeyDown, true);
    return () => {
      window.removeEventListener("mousedown", onPointerDown);
      window.removeEventListener("keydown", onKeyDown, true);
    };
  }, [open, onClose]);

  if (!open) return null;

  const clampedX = typeof window !== "undefined" ? Math.max(8, Math.min(x, window.innerWidth - 280)) : x;
  const clampedY = typeof window !== "undefined" ? Math.max(8, Math.min(y, window.innerHeight - 240)) : y;

  const onMenuKeyDown = (event: React.KeyboardEvent<HTMLDivElement>) => {
    const items = Array.from(menuRef.current?.querySelectorAll<HTMLButtonElement>('[role="menuitem"]') ?? []);
    if (items.length === 0) return;
    const index = items.indexOf(document.activeElement as HTMLButtonElement);
    let next: number | null = null;
    if (event.key === "ArrowDown") next = index < 0 ? 0 : (index + 1) % items.length;
    else if (event.key === "ArrowUp") next = index <= 0 ? items.length - 1 : index - 1;
    else if (event.key === "Home") next = 0;
    else if (event.key === "End") next = items.length - 1;
    if (next !== null) {
      event.preventDefault();
      items[next]?.focus();
    }
  };

  const disabled = atLimit || busy;

  return (
    <div
      ref={menuRef}
      role="menu"
      aria-label="Ações da aba do projeto"
      className="tab-context-menu"
      style={{ left: `${clampedX}px`, top: `${clampedY}px` }}
      onKeyDown={onMenuKeyDown}
      // The Menu key and Shift+F10 also send a contextmenu to the focused item: never the WebView menu over this one.
      onContextMenu={(event) => event.preventDefault()}
    >
      <div className="tab-context-menu__heading">Abrir terminal em...</div>
      {authorizedProjects.length === 0 ? (
        <div role="menuitem" aria-disabled="true" tabIndex={-1} className="tab-context-menu__empty">
          Nenhum diretório autorizado salvo
        </div>
      ) : (
        authorizedProjects.map((dir) => {
          const isCurrent = currentProjectPath ? normalizeWin(dir) === normalizeWin(currentProjectPath) : false;
          return (
            <button
              key={dir}
              type="button"
              role="menuitem"
              tabIndex={-1}
              aria-disabled={disabled || undefined}
              className="tab-context-menu__item"
              title={dir}
              onClick={() => {
                if (disabled) return;
                onClose();
                onSelectProject(dir);
              }}
            >
              <Icon name="terminal" size="sm" />
              <span className="tab-context-menu__text">
                <span className="tab-context-menu__name">
                  {folderLabel(dir)}
                  {isCurrent ? " (atual)" : ""}
                </span>
                <span className="tab-context-menu__path">{dir}</span>
              </span>
            </button>
          );
        })
      )}
      {atLimit && (
        <div className="tab-context-menu__note">Limite de 4 terminais simultâneos atingido.</div>
      )}
      {onOpenOtherProject && (
        <>
          <div className="tab-context-menu__separator" role="separator" />
          <button
            type="button"
            role="menuitem"
            tabIndex={-1}
            aria-disabled={busy || undefined}
            className="tab-context-menu__item"
            onClick={() => {
              if (busy) return;
              onClose();
              onOpenOtherProject();
            }}
          >
            <Icon name="shield" size="sm" />
            <span className="tab-context-menu__name">Outro diretório (Abrir projeto)...</span>
          </button>
        </>
      )}
    </div>
  );
};
