/**
 * Accessible modal dialog (Marco 4D.3 — fundação visual e acessível).
 *
 * - Rendered in a portal over the application, with role="dialog", aria-modal and aria-labelledby.
 * - While open, the application root (#root) is made inert, so pointer and keyboard cannot reach it.
 * - Focus moves into the dialog on open and stays there: Tab / Shift+Tab cycle inside it.
 * - Escape, the close button and a backdrop click close it only when `dismissible` is true.
 * - On close, focus returns deterministically to the element that had it when the dialog opened.
 */
import React, { useCallback, useEffect, useId, useRef } from "react";
import { createPortal } from "react-dom";
import { Icon } from "./Icon";

const FOCUSABLE_SELECTOR = [
  "a[href]",
  "button:not([disabled])",
  "input:not([disabled]):not([type='hidden'])",
  "select:not([disabled])",
  "textarea:not([disabled])",
  "[contenteditable='true']",
  "[tabindex]:not([tabindex='-1'])",
].join(",");

export function getFocusableElements(root: HTMLElement): HTMLElement[] {
  return Array.from(root.querySelectorAll<HTMLElement>(FOCUSABLE_SELECTOR)).filter(
    (el) => el.getAttribute("aria-hidden") !== "true" && !el.closest("[inert]") && !el.closest("[hidden]")
  );
}

export interface DialogProps {
  open: boolean;
  title: string;
  onClose: () => void;
  /** When false, Escape, backdrop and the close button do nothing (e.g. an install in progress). */
  dismissible?: boolean;
  size?: "md" | "lg";
  /** Element to focus on open; defaults to the first focusable element of the body. */
  initialFocusRef?: React.RefObject<HTMLElement | null>;
  footer?: React.ReactNode;
  closeLabel?: string;
  /** Id of the application root made inert while the dialog is open. */
  appRootId?: string;
  children: React.ReactNode;
}

export const Dialog: React.FC<DialogProps> = ({ open, ...rest }) => (open ? <DialogSurface {...rest} /> : null);

const DialogSurface: React.FC<Omit<DialogProps, "open">> = ({
  title,
  onClose,
  dismissible = true,
  size = "md",
  initialFocusRef,
  footer,
  closeLabel = "Fechar",
  appRootId = "root",
  children,
}) => {
  const titleId = useId();
  const dialogRef = useRef<HTMLDivElement>(null);
  const bodyRef = useRef<HTMLDivElement>(null);
  const onCloseRef = useRef(onClose);
  onCloseRef.current = onClose;
  const dismissibleRef = useRef(dismissible);
  dismissibleRef.current = dismissible;

  // Mount: remember the opener, make the app inert, move focus in. Unmount: undo, return focus.
  useEffect(() => {
    const opener = document.activeElement instanceof HTMLElement ? document.activeElement : null;
    const appRoot = document.getElementById(appRootId);
    const madeInert = !!appRoot && !appRoot.hasAttribute("inert");
    if (madeInert) appRoot!.setAttribute("inert", "");

    const dialog = dialogRef.current;
    const target =
      initialFocusRef?.current ??
      (bodyRef.current ? getFocusableElements(bodyRef.current)[0] : undefined) ??
      (dialog ? getFocusableElements(dialog)[0] : undefined) ??
      dialog;
    target?.focus();

    return () => {
      if (madeInert) appRoot!.removeAttribute("inert");
      if (opener && opener.isConnected) opener.focus();
    };
    // Mount/unmount only: the dialog instance owns one open/close cycle.
  }, []);

  const requestClose = useCallback(() => {
    if (dismissibleRef.current) onCloseRef.current();
  }, []);

  const handleKeyDown = useCallback(
    (event: React.KeyboardEvent<HTMLDivElement>) => {
      if (event.key === "Escape") {
        event.preventDefault();
        event.stopPropagation();
        requestClose();
        return;
      }
      if (event.key !== "Tab" || !dialogRef.current) return;

      const items = getFocusableElements(dialogRef.current);
      if (items.length === 0) {
        event.preventDefault();
        dialogRef.current.focus();
        return;
      }
      const first = items[0];
      const last = items[items.length - 1];
      const active = document.activeElement;
      if (event.shiftKey && (active === first || active === dialogRef.current)) {
        event.preventDefault();
        last.focus();
      } else if (!event.shiftKey && active === last) {
        event.preventDefault();
        first.focus();
      }
    },
    [requestClose]
  );

  const handleBackdropMouseDown = useCallback(
    (event: React.MouseEvent<HTMLDivElement>) => {
      if (event.target === event.currentTarget) requestClose();
    },
    [requestClose]
  );

  return createPortal(
    <div className="dialog-backdrop" onMouseDown={handleBackdropMouseDown} data-testid="dialog-backdrop">
      <div
        ref={dialogRef}
        className={size === "lg" ? "dialog dialog--lg" : "dialog"}
        role="dialog"
        aria-modal="true"
        aria-labelledby={titleId}
        tabIndex={-1}
        onKeyDown={handleKeyDown}
      >
        <header className="dialog__header">
          <h2 id={titleId} className="dialog__title">
            {title}
          </h2>
          <button
            type="button"
            className="btn btn--ghost btn--icon"
            onClick={requestClose}
            disabled={!dismissible}
            aria-label={closeLabel}
            title={closeLabel}
          >
            <Icon name="close" />
          </button>
        </header>
        <div ref={bodyRef} className="dialog__body">
          {children}
        </div>
        {footer ? <footer className="dialog__footer">{footer}</footer> : null}
      </div>
    </div>,
    document.body
  );
};
