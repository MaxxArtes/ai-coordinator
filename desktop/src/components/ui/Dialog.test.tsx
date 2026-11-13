import React, { useState } from "react";
import { describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { Dialog } from "./Dialog";

function renderInAppRoot(ui: React.ReactElement) {
  const root = document.createElement("div");
  root.id = "root";
  document.body.appendChild(root);
  return render(ui, { container: root });
}

const Harness: React.FC<{ dismissible?: boolean; onClose?: () => void }> = ({ dismissible = true, onClose }) => {
  const [open, setOpen] = useState(false);
  return (
    <>
      <button type="button" onClick={() => setOpen(true)}>
        Abrir
      </button>
      <Dialog
        open={open}
        title="Diálogo de teste"
        dismissible={dismissible}
        onClose={() => {
          onClose?.();
          setOpen(false);
        }}
      >
        <button type="button">Primeiro</button>
        <button type="button">Segundo</button>
      </Dialog>
    </>
  );
};

async function openDialog(props: React.ComponentProps<typeof Harness> = {}) {
  const user = userEvent.setup();
  renderInAppRoot(<Harness {...props} />);
  await user.click(screen.getByRole("button", { name: "Abrir" }));
  return user;
}

describe("Dialog", () => {
  it("abre como diálogo modal rotulado pelo título", async () => {
    await openDialog();
    const dialog = screen.getByRole("dialog", { name: "Diálogo de teste" });
    expect(dialog).toHaveAttribute("aria-modal", "true");
  });

  it("move o foco para o primeiro controle do conteúdo", async () => {
    await openDialog();
    expect(screen.getByRole("button", { name: "Primeiro" })).toHaveFocus();
  });

  it("mantém Tab e Shift+Tab presos dentro do diálogo", async () => {
    const user = await openDialog();
    await user.tab();
    expect(screen.getByRole("button", { name: "Segundo" })).toHaveFocus();
    await user.tab(); // wraps to the first focusable element of the dialog (close button)
    expect(screen.getByRole("button", { name: "Fechar" })).toHaveFocus();
    await user.tab({ shift: true }); // wraps back to the last one
    expect(screen.getByRole("button", { name: "Segundo" })).toHaveFocus();
  });

  it("fecha com Esc e devolve o foco a quem abriu", async () => {
    const onClose = vi.fn();
    const user = await openDialog({ onClose });
    await user.keyboard("{Escape}");
    expect(onClose).toHaveBeenCalledTimes(1);
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Abrir" })).toHaveFocus();
  });

  it("fecha pelo fundo e pelo botão Fechar quando dispensável", async () => {
    const onClose = vi.fn();
    const user = await openDialog({ onClose });
    fireEvent.mouseDown(screen.getByTestId("dialog-backdrop"));
    expect(onClose).toHaveBeenCalledTimes(1);

    await user.click(screen.getByRole("button", { name: "Abrir" }));
    await user.click(screen.getByRole("button", { name: "Fechar" }));
    expect(onClose).toHaveBeenCalledTimes(2);
  });

  it("não fecha por Esc, fundo ou botão quando não dispensável", async () => {
    const onClose = vi.fn();
    const user = await openDialog({ onClose, dismissible: false });
    await user.keyboard("{Escape}");
    fireEvent.mouseDown(screen.getByTestId("dialog-backdrop"));
    expect(screen.getByRole("button", { name: "Fechar" })).toBeDisabled();
    expect(onClose).not.toHaveBeenCalled();
    expect(screen.getByRole("dialog")).toBeInTheDocument();
  });

  it("torna a raiz do aplicativo inerte apenas enquanto está aberto", async () => {
    const user = await openDialog();
    const root = document.getElementById("root")!;
    expect(root).toHaveAttribute("inert");
    await user.keyboard("{Escape}");
    expect(root).not.toHaveAttribute("inert");
  });
});
