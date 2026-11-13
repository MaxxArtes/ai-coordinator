import React from "react";
import { Icon } from "../ui/Icon";
import type { SettingsCategory } from "./categories";

/**
 * Structure of a category that is not configurable yet (decision D4): it states its scope and planned stage,
 * offers no controls, and never claims that anything is saved or applied.
 */
export const CategoryPanel: React.FC<{ category: SettingsCategory }> = ({ category }) => (
  <>
    <div className="settings__panel-header">
      <h3 className="settings__heading">{category.label}</h3>
      <span className="badge badge--planned">Planejado{category.stage ? ` — ${category.stage}` : ""}</span>
    </div>
    <p className="settings__summary">{category.summary}</p>

    <div className="callout callout--info">
      <p className="callout__title">
        <Icon name="info" size="sm" />
        <span>Ainda não configurável nesta versão experimental</span>
      </p>
      <p className="callout__body">
        Nenhuma preferência desta categoria é salva ou aplicada. As opções abaixo serão habilitadas com validação no
        backend quando a etapa correspondente for implementada.
      </p>
    </div>

    <div>
      <h4 className="settings__section-title">Escopo previsto</h4>
      <ul className="settings__list">
        {category.items.map((item) => (
          <li key={item.label}>
            <span>{item.label}</span>
            {item.unsupported && (
              <>
                {" "}
                <span className="badge badge--unsupported">Não suportado nesta versão</span>
                <span className="settings__note">{item.unsupported}</span>
              </>
            )}
          </li>
        ))}
      </ul>
    </div>
  </>
);
