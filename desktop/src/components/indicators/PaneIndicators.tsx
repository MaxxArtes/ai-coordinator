import React, { useEffect, useId, useRef, useState } from "react";
import { IndicatorRing } from "./IndicatorRing";
import { indicatorsName, normalizeReading, readingDescription, terminalReadings, type IndicatorReading } from "./readings";

export interface PaneIndicatorsProps {
  terminalName: string;
  readings?: IndicatorReading[];
  /** Visual-check fixture only; the application never opens a popover by itself. */
  initiallyOpen?: boolean;
}

/** One Tab stop for three indicators. A non-modal explanation, with no terminal commands or invented values. */
export const PaneIndicators: React.FC<PaneIndicatorsProps> = ({ terminalName, readings = terminalReadings(), initiallyOpen = false }) => {
  const values = readings.map(normalizeReading);
  const [open, setOpen] = useState(initiallyOpen);
  const rootRef = useRef<HTMLDivElement>(null);
  const buttonRef = useRef<HTMLButtonElement>(null);
  const popoverRef = useRef<HTMLDivElement>(null);
  const pinnedRef = useRef(initiallyOpen);
  const closeTimer = useRef<ReturnType<typeof setTimeout>>();
  const id = useId();
  const announceOpen = () => document.dispatchEvent(new CustomEvent("pane-indicators-open", { detail: id }));
  const titleId = `${id}-title`;
  const detailsId = `${id}-details`;
  const cancelClose = () => clearTimeout(closeTimer.current);
  const close = () => { cancelClose(); pinnedRef.current = false; setOpen(false); };

  useEffect(() => () => clearTimeout(closeTimer.current), []);
  useEffect(() => {
    const otherOpened = (event: Event) => {
      if ((event as CustomEvent<string>).detail !== id) close();
    };
    document.addEventListener("pane-indicators-open", otherOpened);
    return () => document.removeEventListener("pane-indicators-open", otherOpened);
  }, [id]);
  useEffect(() => {
    if (!open) return;
    const escape = (event: KeyboardEvent) => {
      if (event.key !== "Escape") return;
      // A modal or an inactive view owns the keyboard. Close our background popup without taking its Escape/focus.
      if (rootRef.current?.closest("[inert], [hidden], .is-invisible")) { close(); return; }
      event.preventDefault();
      event.stopPropagation();
      close();
      buttonRef.current?.focus();
    };
    const outside = (event: PointerEvent) => {
      if (!rootRef.current?.contains(event.target as Node)) close();
    };
    document.addEventListener("keydown", escape, true);
    document.addEventListener("pointerdown", outside, true);
    return () => {
      document.removeEventListener("keydown", escape, true);
      document.removeEventListener("pointerdown", outside, true);
    };
  }, [open]);

  const activate = (event: React.MouseEvent<HTMLButtonElement>) => {
    cancelClose();
    if (open && pinnedRef.current) { close(); return; }
    announceOpen();
    pinnedRef.current = true;
    setOpen(true);
    if (event.detail === 0) requestAnimationFrame(() => popoverRef.current?.focus());
  };

  return (
    <div className="pane-indicators" ref={rootRef}
      onMouseEnter={() => { cancelClose(); announceOpen(); setOpen(true); }}
      onMouseLeave={() => {
        cancelClose();
        if (!pinnedRef.current) closeTimer.current = setTimeout(() => setOpen(false), 150);
      }}
      onBlur={(event) => {
        if (!event.currentTarget.contains(event.relatedTarget as Node | null)) close();
      }}
    >
      <button ref={buttonRef} type="button" className="pane-indicators__button"
        aria-label={indicatorsName(terminalName, values)} aria-haspopup="dialog" aria-expanded={open}
        aria-controls={open ? id : undefined} onClick={activate}
      >
        {values.map((reading) => <IndicatorRing key={reading.kind} reading={reading} />)}
      </button>
      {open && (
        <div ref={popoverRef} id={id} className="pane-indicators__popover" role="dialog" aria-modal="false"
          aria-labelledby={titleId} aria-describedby={detailsId} tabIndex={-1}
        >
          <p id={titleId} className="pane-indicators__title">Indicadores do {terminalName}</p>
          <ul id={detailsId} className="pane-indicators__readings">
            {values.map((reading) => <li key={reading.kind}>{readingDescription(reading)}</li>)}
          </ul>
        </div>
      )}
    </div>
  );
};
