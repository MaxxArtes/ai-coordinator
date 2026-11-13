import React, { useId } from "react";
import { Icon } from "../ui/Icon";

export interface AuthorizationScreenProps {
  profileName: string;
  inputPath: string;
  onInputPathChange: (value: string) => void;
  onSubmit: (path: string) => void;
}

/** First-run gate: the operator must explicitly authorize a project directory before any terminal starts. */
export const AuthorizationScreen: React.FC<AuthorizationScreenProps> = ({
  profileName,
  inputPath,
  onInputPathChange,
  onSubmit,
}) => {
  const inputId = useId();
  const hintId = useId();

  const handleSubmit = (event: React.FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    onSubmit(inputPath);
  };

  return (
    <div className="screen-center">
      <form className="card" onSubmit={handleSubmit} aria-labelledby={`${inputId}-title`}>
        <h2 id={`${inputId}-title`} className="card__title">
          <Icon name="shield" />
          <span>Autorização de projeto obrigatória</span>
        </h2>
        <p className="card__text">
          Nenhum projeto está autorizado para o perfil ativo (<code>{profileName}</code>). Para assegurar a contenção
          atômica do ConPTY e proteger o sistema de arquivos do host, informe e autorize explicitamente o diretório de
          trabalho antes de iniciar o terminal.
        </p>
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
            autoFocus
          />
          <span id={hintId} className="field__hint">
            Apenas diretórios autorizados podem ser usados como raiz dos terminais.
          </span>
        </div>
        <div className="card__actions">
          <button type="submit" className="btn btn--primary">
            Autorizar diretório e iniciar terminal
          </button>
        </div>
      </form>
    </div>
  );
};
