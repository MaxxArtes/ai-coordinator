/**
 * Inline question before an action that cannot be taken back or that widens access (Marco 4D.3): replacing or removing
 * a vault credential, authorizing a broad directory (Etapa 8, L7).
 * - role="alertdialog" inside the panel it is about; the owner moves the focus to the safe answer ("Cancelar").
 * - A pointer click only answers after CONFIRM_ARM_DELAY_MS: the risky answer can appear under the pointer that opened
 *   the question, and the second click of a double click must not answer it unread. Keyboard activation is not
 *   delayed (the focus starts on Cancelar, so reaching the risky answer already takes a deliberate key press).
 */
import React, { useEffect, useRef, useState } from "react";
import { Icon } from "./Icon";

/** How long a pointer click waits before it may answer an open question (the same for every question). */
export const CONFIRM_ARM_DELAY_MS = 600;

/** Clock of the arm delay. Monotonic: a correction of the system time must not leave a question unanswerable. */
export const armingClock = { now: (): number => performance.now() };

/**
 * Pointer gate of one question. `arm()` when the question opens; `accepts(event)` tells whether a click may answer it:
 * not before the delay, and not as the next click of a multi-click that started before that. Insisting on the armed
 * button does get through.
 */
export function useArmedAnswer() {
  const armedAtRef = useRef(0);
  const lastClickRef = useRef<number | null>(null);
  const gate = useRef({
    arm: () => {
      armedAtRef.current = armingClock.now() + CONFIRM_ARM_DELAY_MS;
      lastClickRef.current = null;
    },
    accepts: (event: React.MouseEvent): boolean => {
      if (event.detail === 0) return true; // keyboard activation
      const now = armingClock.now();
      const previous = lastClickRef.current;
      lastClickRef.current = now;
      if (now < armedAtRef.current) return false;
      return !(event.detail > 1 && (previous === null || previous < armedAtRef.current));
    },
  });
  return gate.current;
}

/** Holding Enter down must not walk through a form and its question (submit, cancel, submit again…): only real presses count. */
export const ignoreRepeatedEnter = (event: React.KeyboardEvent) => {
  if (event.key === "Enter" && event.repeat) event.preventDefault();
};

export interface InlineConfirmationProps {
  /** Base of the ids that name and describe the question. */
  idBase: string;
  title: React.ReactNode;
  body: React.ReactNode;
  /** Label of the risky answer. */
  action: string;
  cancelRef: React.RefObject<HTMLButtonElement>;
  onCancel: () => void;
  onConfirm: (event: React.MouseEvent<HTMLButtonElement>) => void;
  cancelClassName?: string;
}

export const InlineConfirmation: React.FC<InlineConfirmationProps> = ({
  idBase,
  title,
  body,
  action,
  cancelRef,
  onCancel,
  onConfirm,
  cancelClassName = "btn btn--sm btn--ghost",
}) => {
  // Shows the arm delay: until it has passed, the risky answer looks unavailable to the pointer.
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
        <span>{title}</span>
      </p>
      <p id={`${idBase}-confirm-body`} className="callout__body">
        {body}
      </p>
      <div className="row-end">
        <button ref={cancelRef} type="button" className={cancelClassName} onClick={onCancel}>
          Cancelar
        </button>
        <button
          type="button"
          className={armed ? "btn btn--sm btn--danger" : "btn btn--sm btn--danger btn--arming"}
          onClick={onConfirm}
        >
          {action}
        </button>
      </div>
    </div>
  );
};
