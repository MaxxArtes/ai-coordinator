import React, { useEffect, useState } from "react";
import { mainViewPanelId, mainViewTabId } from "./MainNav";

type Mission = { id: string; title: string; status: string };
type Task = { id: string; goal: string; status: string };
type Node = { node_id: string; title: string; description: string; status: string; assigned_role: string };
type Dag = {
  revision: number;
  nodes: Record<string, Node>;
  edges: Array<{ from_node: string; to_node: string }>;
  node_order: string[];
  ready_node_ids: string[];
};
type MissionDetails = Mission & { tasks: Task[]; dag: Dag | null };

interface MissionsViewProps {
  active: boolean;
  projectPath?: string;
  invoke?: (command: string, args: Record<string, string>) => Promise<unknown>;
}

const statuses: Record<string, string> = {
  active: "Ativa",
  complete: "Concluída",
  pending: "Pendente",
  ready: "Pronto",
  in_progress: "Em andamento",
  completed: "Concluído",
  blocked: "Bloqueado",
  skipped: "Ignorado",
};

function DagView({ dag }: { dag: Dag }) {
  const predecessors = new Map<string, string[]>();
  for (const edge of dag.edges) {
    const incoming = predecessors.get(edge.to_node) ?? [];
    incoming.push(edge.from_node);
    predecessors.set(edge.to_node, incoming);
  }

  const depths = new Map<string, number>();
  const columns: string[][] = [];
  for (const id of dag.node_order) {
    const depth = (predecessors.get(id) ?? []).reduce(
      (max, parent) => Math.max(max, (depths.get(parent) ?? 0) + 1),
      0,
    );
    depths.set(id, depth);
    (columns[depth] ??= []).push(id);
  }
  const ready = new Set(dag.ready_node_ids);

  return (
    <section aria-label="Grafo de dependências">
      <h3>Dependências · revisão {dag.revision}</h3>
      {columns.length === 0 && <p>O grafo está vazio.</p>}
      <div className="missions-view__graph">
        {columns.map((ids, index) => (
          <ul key={index} className="missions-view__column">
            {ids.map((id) => {
              const node = dag.nodes[id];
              const parents = predecessors.get(id) ?? [];
              return (
                <li key={id} className="missions-view__node">
                  <details>
                    <summary>
                      <strong>{node.title}</strong>
                      <span>{statuses[node.status] ?? node.status}</span>
                      {ready.has(id) && <span>Pronto para avançar</span>}
                    </summary>
                    {node.description && <p>{node.description}</p>}
                    {node.assigned_role === "human" && <p>Responsável humano</p>}
                    {parents.length > 0 && (
                      <p>Depende de {parents.map((parent) => dag.nodes[parent].title).join(", ")}</p>
                    )}
                  </details>
                </li>
              );
            })}
          </ul>
        ))}
      </div>
    </section>
  );
}

export function MissionsView({ active, projectPath, invoke }: MissionsViewProps) {
  const [missions, setMissions] = useState<Mission[]>();
  const [selected, setSelected] = useState("");
  const [detail, setDetail] = useState<MissionDetails>();
  const [error, setError] = useState("");
  const [request, setRequest] = useState(0);

  useEffect(() => {
    if (!active || !projectPath || !invoke) return;
    let current = true;
    setMissions(undefined);
    setError("");
    void invoke("mission_list", { projectPath }).then((response) => {
      if (!current) return;
      const items = response as Mission[];
      setMissions(items);
      setSelected((value) => items.some((mission) => mission.id === value) ? value : items[0]?.id ?? "");
    }).catch(() => {
      if (current) setError("Não foi possível carregar as missões.");
    });
    return () => { current = false; };
  }, [active, invoke, projectPath, request]);

  useEffect(() => {
    if (!active || !projectPath || !invoke || !selected) return;
    let current = true;
    setDetail(undefined);
    void invoke("mission_get", { projectPath, missionId: selected }).then((response) => {
      if (current) setDetail(response as MissionDetails);
    }).catch(() => {
      if (current) setError("Não foi possível carregar a missão selecionada.");
    });
    return () => { current = false; };
  }, [active, invoke, projectPath, request, selected]);

  return (
    <section
      role="tabpanel"
      id={mainViewPanelId("missoes")}
      aria-labelledby={mainViewTabId("missoes")}
      hidden={!active}
      tabIndex={0}
      className="app-view missions-view"
      data-f6-region="view"
      data-f6-entry=""
    >
      <div className="missions-view__heading">
        <h2>Missões</h2>
        {projectPath && invoke && (
          <button type="button" onClick={() => setRequest((value) => value + 1)}>Atualizar</button>
        )}
      </div>
      {!projectPath && <p>Abra um projeto para consultar suas missões.</p>}
      {projectPath && !invoke && <p>Aguardando conexão com o Core.</p>}
      {error && <p role="alert">{error}</p>}
      {projectPath && invoke && !error && !missions && <p role="status">Carregando missões…</p>}
      {projectPath && invoke && missions?.length === 0 && !error && <p>Este projeto ainda não tem missões registradas.</p>}
      {projectPath && invoke && missions && missions.length > 0 && (
        <label className="missions-view__select">
          Missão
          <select value={selected} onChange={(event) => { setError(""); setSelected(event.target.value); }}>
            {missions.map((mission) => <option key={mission.id} value={mission.id}>{mission.title}</option>)}
          </select>
        </label>
      )}
      {projectPath && invoke && !error && missions && selected && detail?.id !== selected && <p role="status">Carregando missão…</p>}
      {projectPath && invoke && !error && missions && detail?.id === selected && (
        <>
          <p>{statuses[detail.status] ?? detail.status}</p>
          {detail.dag ? <DagView dag={detail.dag} /> : <p>Esta missão ainda não tem um grafo.</p>}
          <h3>Tarefas vinculadas</h3>
          <ul>
            {detail.tasks.map((task) => (
              <li key={task.id}>{task.goal} · {statuses[task.status] ?? task.status}</li>
            ))}
          </ul>
        </>
      )}
    </section>
  );
}
