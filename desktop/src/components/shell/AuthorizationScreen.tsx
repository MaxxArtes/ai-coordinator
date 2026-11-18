import React, { useId } from "react";
import { Icon } from "../ui/Icon";

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
}

/**
 * Project gate: no terminal starts outside an authorized directory. With authorized projects it is the project chooser
 * (one click opens one) and still lets the operator authorize another directory. It promises nothing the policy does
 * not do: the terminal runs with the operator's own permissions, without file system isolation.
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
}) => {
  const inputId = useId();
  const hintId = useId();
  const hasProjects = authorizedProjects.length > 0 && !!onOpenProject;

  const handleSubmit = (event: React.FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    if (busy) return;
    onSubmit(inputPath);
  };

  return (
    <div className="screen-center" data-f6-region="view">
      <section className="card" aria-labelledby={`${inputId}-title`} aria-busy={busy || undefined}>
        <h2 id={`${inputId}-title`} className="card__title">
          <Icon name="shield" />
          <span>{hasProjects ? "Escolha o projeto" : "Autorização de projeto obrigatória"}</span>
        </h2>
        {note && <p className="card__text">{note}</p>}
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
                    aria-disabled={busy || undefined}
                    autoFocus={index === 0}
                    data-f6-entry={index === 0 ? "" : undefined}
                    onClick={() => {
                      if (!busy) onOpenProject?.(path);
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
              type="text"
              className="input"
              value={inputPath}
              onChange={(e) => onInputPathChange(e.target.value)}
              placeholder="Ex.: C:\Users\voce ou D:\meu-projeto"
              aria-describedby={hintId}
              autoComplete="off"
              spellCheck={false}
              autoFocus={!hasProjects}
              data-f6-entry={hasProjects ? undefined : ""}
            />
            <span id={hintId} className="field__hint">
              O diretório precisa existir neste computador.
            </span>
          </div>
          <div className="card__actions">
            <button type="submit" className="btn btn--primary" aria-disabled={busy || undefined}>
              Autorizar diretório e iniciar terminal
            </button>
          </div>
        </form>
      </section>
    </div>
  );
};
