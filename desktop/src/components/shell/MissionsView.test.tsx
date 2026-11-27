import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import { MissionsView } from "./MissionsView";

const missions = [
  { id: "anchor", title: "Continuidade", status: "active" },
  { id: "other", title: "Revisão", status: "complete" },
];

describe("MissionsView", () => {
  it("renders all nodes, dependencies, linked tasks and the advisory ready set", async () => {
    const user = userEvent.setup();
    const call = vi.fn(async (command: string, params: Record<string, string>) => {
      if (command === "mission_list") return missions;
      if (params.missionId === "other") return { ...missions[1], dag: null, tasks: [] };
      return {
        ...missions[0],
        tasks: [{ id: "review", goal: "Revisar evidência", status: "blocked" }],
        dag: {
          revision: 2,
          node_order: ["first", "second", "last"],
          nodes: {
            first: { node_id: "first", title: "Coletar", status: "completed", assigned_role: "worker" },
            second: { node_id: "second", title: "Validar", status: "pending", assigned_role: "human" },
            last: { node_id: "last", title: "Revisar", status: "blocked", assigned_role: "worker" },
          },
          edges: [{ from_node: "first", to_node: "second" }, { from_node: "second", to_node: "last" }],
          ready_node_ids: ["second"],
        },
      };
    });
    render(<MissionsView active projectPath="C:/project" invoke={call} />);
    await screen.findByText("Dependências · revisão 2");
    const graph = screen.getByRole("region", { name: "Grafo de dependências" });
    const items = within(graph).getAllByRole("listitem");
    expect(items).toHaveLength(3);
    expect(items[0]).toHaveTextContent("ColetarConcluído");
    expect(items[1]).toHaveTextContent("ValidarPendentePronto para avançar");
    expect(items[2]).toHaveTextContent("RevisarBloqueado");
    await user.click(within(graph).getByText("Validar"));
    expect(within(graph).getByText("Depende de Coletar")).toBeVisible();
    expect(within(graph).getByText("Responsável humano")).toBeVisible();
    expect(screen.getByText("Revisar evidência · Bloqueado")).toBeVisible();
    await user.selectOptions(screen.getByRole("combobox", { name: "Missão" }), "other");
    expect(await screen.findByText("Esta missão ainda não tem um grafo.")).toBeVisible();
    expect(call.mock.calls.every(([method]) => ["mission_list", "mission_get"].includes(method))).toBe(true);
  });

  it("shows an error when the Core cannot list missions", async () => {
    const call = vi.fn().mockRejectedValue(new Error("unavailable"));
    render(<MissionsView active projectPath="C:/project" invoke={call} />);
    expect(await screen.findByRole("alert")).toHaveTextContent("Não foi possível carregar as missões.");
  });
});
