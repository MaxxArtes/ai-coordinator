import React, { useId, useLayoutEffect, useRef } from "react";
import { Icon } from "../ui/Icon";
import { InlineConfirmation, ignoreRepeatedEnter, useArmedAnswer } from "../ui/InlineConfirmation";

export interface AuthorizationScreenProps {
  profileName: string;
  /** Projects already authorized for the active profile: opened with one click, without authorizing again. */
  authorizedProjects?: string[];
  /** Why this screen is shown instead of a terminal (automatic opening off, the open project revoked…). */
  note?: string;
  inputPath: string;
  onInputPathChange: (value: string) => void;
  onSubmit: (path: string) => void;
  onOpenProject?: (path: string) => void;
  /** A project is being opened or authorized: further requests are ignored until it ends. */
  busy?: boolean;
  /**
   * The directory, as it was sent, that the host refused as broad (disk or share root, the whole home folder: L5).
   * While set, the screen asks for an explicit confirmation and every other request is ignored.
   */
  broadScopePath?: string | null;
  /** Explicit yes: the App repeats the authorization with confirmBroad (L7). */
  onConfirmBroadScope?: () => void;
  /** Cancelar or Escape: nothing is authorized. */
  onCancelBroadScope?: () => void;
}

/**
 * Project gate: no terminal starts outside an authorized directory. With authorized projects it is the project chooser
 * (one click opens one) and still lets the operator authorize another directory. It promises nothing the policy does
 * not do: the terminal runs with the operator's own permissions, without file system isolation.
 *
 * A broad directory is only authorized after an explicit question (L7): the safe answer (Cancelar) gets the focus,
 * Escape answers it, and a pointer click on the risky answer waits the same arm delay as the vault's questions.
 */
export const AuthorizationScreen: React.FC<AuthorizationScreenProps> = ({
  profileName,
  authorizedProjects = [],
  note,
  inputPath,
  onInputPathChange,
  onSubmit,
  onOpenProject,
  busy = false,
  broadScopePath = null,
  onConfirmBroadScope,
  onCancelBroadScope,
}) => {
  const inputId = useId();
  const hintId = useId();
  const noteId = useId();
  const questionId = useId();
  const inputRef = useRef<HTMLInputElement>(null);
  const submitRef = useRef<HTMLButtonElement>(null);
  const cancelRef = useRef<HTMLButtonElement>(null);
  const answerGate = useArmedAnswer();
  const asking = broadScopePath !== null;
  const locked = busy || asking;
  const hasProjects = authorizedProjects.length > 0 && !!onOpenProject;
  // No notice banner comes with this screen: the reason describes the control that gets the focus, so a screen
  // reader announces it on arrival (a live region born with the text would not be announced reliably).
  const noteRef = note ? noteId : undefined;

  // The question opens armed for the keyboard only, with the focus on the safe answer. Before paint: a click that
  // lands in the same frame is measured against this question, not against an older one.
  useLayoutEffect(() => {
    if (!asking) return;
    answerGate.arm();
    cancelRef.current?.focus();
  }, [asking, broadScopePath]);

  // The question's buttons unmount with it: the focus moves in the same commit, never waits on <body>.
  const focusAfterRef = useRef<HTMLElement | null>(null);
  useLayoutEffect(() => {
    const target = focusAfterRef.current;
    if (asking || !target) return;
    focusAfterRef.current = null;
    if (target.isConnected) target.focus();
  }, [asking]);

  const cancelQuestion = () => {
    if (!asking || busy) return;
    // Back to the path, so a narrower directory can be typed right away.
    focusAfterRef.current = inputRef.current;
    onCancelBroadScope?.();
  };

  // Escape answers the question from anywhere on this screen (Cancelar), as in Configurações.
  const cancelRefHandler = useRef(cancelQuestion);
  cancelRefHandler.current = cancelQuestion;
  const handleKeyDown = (event: React.KeyboardEvent) => {
    ignoreRepeatedEnter(event);
    if (event.key !== "Escape" || !asking) return;
    event.preventDefault();
    event.stopPropagation();
    cancelRefHandler.current();
  };

  const handleConfirm = (event: React.MouseEvent<HTMLButtonElement>) => {
    if (!asking || busy || !answerGate.accepts(event)) return;
    // The submit button stays (aria-disabled while busy): the focus waits there for the outcome.
    focusAfterRef.current = submitRef.current;
    onConfirmBroadScope?.();
  };

  const handleSubmit = (event: React.FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    if (locked) return;
    onSubmit(inputPath);
  };

  return (
    <div className="screen-center" data-f6-region="view">
      <section
        className="card"
        aria-labelledby={`${inputId}-title`}
        aria-busy={busy || undefined}
        onKeyDown={handleKeyDown}
      >
        <h2 id={`${inputId}-title`} className="card__title">
          <Icon name="shield" />
          <span>{hasProjects ? "Escolha o projeto" : "Autorização de projeto obrigatória"}</span>
        </h2>
        {note && (
          <p id={noteId} className="card__text">
            {note}
          </p>
        )}
        {hasProjects ? (
          <>
            <p className="card__text">
              Projetos já autorizados para o perfil ativo (<code>{profileName}</code>):
            </p>
            <ul className="authorization__projects">
              {authorizedProjects.map((path, index) => (
                <li key={path}>
                  <button
                    type="button"
                    className="btn btn--sm"
                    aria-disabled={locked || undefined}
                    autoFocus={index === 0 && !asking}
                    aria-describedby={index === 0 ? noteRef : undefined}
                    data-f6-entry={index === 0 ? "" : undefined}
                    onClick={() => {
                      if (!locked) onOpenProject?.(path);
                    }}
                  >
                    Abrir {path}
                  </button>
                </li>
              ))}
            </ul>
          </>
        ) : (
          <p className="card__text">
            Nenhum projeto está autorizado para o perfil ativo (<code>{profileName}</code>).
          </p>
        )}
        <p className="card__text">
          Somente diretórios autorizados podem ser raiz dos terminais. O terminal usa as permissões do seu usuário do
          Windows; não há isolamento do sistema de arquivos.
        </p>
        <form onSubmit={handleSubmit} aria-label={hasProjects ? "Autorizar outro diretório" : "Autorizar diretório"}>
          {hasProjects && <h3 className="authorization__subtitle">Autorizar outro diretório</h3>}
          <div className="field">
            <label className="field__label" htmlFor={inputId}>
              Caminho do projeto no host
            </label>
            <input
              id={inputId}
              ref={inputRef}
              type="text"
              className="input"
              value={inputPath}
              onChange={(e) => onInputPathChange(e.target.value)}
              // The question is about the path as it was sent: it cannot change under it.
              readOnly={asking}
              placeholder="Ex.: D:\meu-projeto ou C:\Users\voce\projetos\app"
              aria-describedby={!hasProjects && noteRef ? `${noteRef} ${hintId}` : hintId}
              autoComplete="off"
              spellCheck={false}
              autoFocus={!hasProjects && !asking}
              data-f6-entry={hasProjects ? undefined : ""}
            />
            <span id={hintId} className="field__hint">
              O diretório precisa existir neste computador.
            </span>
          </div>
          <div className="card__actions">
            <button ref={submitRef} type="submit" className="btn btn--primary" aria-disabled={locked || undefined}>
              Autorizar diretório e iniciar terminal
            </button>
          </div>
          {/* Below the submit button, so the risky answer does not take the place of the button just clicked. */}
          {asking && (
            <InlineConfirmation
              idBase={questionId}
              title={`Autorizar ${broadScopePath} com acesso amplo?`}
              body={
                <>
                  Este diretório é a raiz de um disco ou de um compartilhamento, ou a sua pasta pessoal inteira. Os
                  terminais poderão abrir qualquer pasta dentro dele, com as permissões do seu usuário do Windows.
                  Prefira autorizar só a pasta do projeto.
                </>
              }
              action="Autorizar acesso amplo"
              cancelRef={cancelRef}
              cancelClassName="btn btn--sm"
              onCancel={cancelQuestion}
              onConfirm={handleConfirm}
            />
          )}
        </form>
      </section>
    </div>
  );
};
