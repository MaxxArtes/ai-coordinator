"""Shared task state and bounded CLI workers. Standard library; existing CLI auth."""
from __future__ import annotations
import argparse
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import subprocess
import sys
import tomllib
import uuid
from .availability import Health
from . import runtime
from .data import default_data_dir, initialize_data_dir

FIELDS = {"summary", "decisions", "changed_files", "checks", "pending", "next_step"}

def now():
    return dt.datetime.now(dt.timezone.utc).isoformat()

def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8-sig"))

def emit(value):
    print(json.dumps(value, ensure_ascii=False, indent=2))

def scrub(text):
    text = re.sub(r"(?i)\b(?:sk-[a-z0-9_-]{24,}|AIza[a-z0-9_-]{30,}|gh[pousr]_[a-z0-9]{30,})\b", "[REDACTED]", text)
    text = re.sub(r'(?i)("(?:authorization|set-cookie|cookie|x-api-key|api_key|password)"\s*:\s*)"(?:\\.|[^"\\])*"', r'\1"[REDACTED]"', text)
    # Tokens do GitLab e chaves das APIs locais (Web2API/Gemini web).
    text = re.sub(r"\b(?:glpat-[A-Za-z0-9_.-]{20,}|(?:w2a|gw)-[0-9a-f]{64})\b", "[REDACTED]", text)
    # senha=..., PASSWORD: ... (estilo .env/Python) e usuario:senha@ em URIs de conexao.
    text = re.sub(r"(?i)\b((?:senha|password|passwd|pwd|secret|api[_-]?key|token)\s*[=:]\s*['\"]?)[^\s'\"]{4,}", r"\1[REDACTED]", text)
    text = re.sub(r"(://[^:/@\s]+:)[^@\s]+@", r"\1[REDACTED]@", text)
    return re.sub(r'(?i)\bBearer\s+[a-zA-Z0-9._-]{12,}', 'Bearer [REDACTED]', text)

def project_path(value):
    p = Path(value).expanduser().resolve()
    if not p.is_dir():
        raise ValueError("Diretorio do projeto nao existe")
    return os.path.normcase(str(p))

def validate_id(value):
    if not re.fullmatch(r"[0-9a-f]{16}", value):
        raise ValueError("ID invalido")
    return value

def atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    tmp.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)

def scope(project, initialize=False):
    directory = Path(project_path(project))
    for ancestor in [directory, *directory.parents]:
        marker = ancestor / '.ai-memory.toml'
        if marker.exists():
            obj = tomllib.loads(marker.read_text(encoding='utf-8-sig'))
            if not isinstance(obj.get('workspace'), str) or not isinstance(obj.get('project'), str):
                raise ValueError(f'Marcador existe sem workspace/project completos; preserve e revise: {marker}')
            return {'workspace': obj['workspace'], 'project': obj['project'], 'marker': str(marker), 'created': False}
    if not initialize:
        return {'marker': None, 'next_step': 'Use scope --init antes de abrir uma nova sessao para declarar o escopo'}
    slug = re.sub(r'[^a-z0-9-]+', '-', directory.name.lower()).strip('-')[:40] or 'project'
    digest = hashlib.sha256(str(directory).encode('utf-8')).hexdigest()[:8]
    name = f'{slug}-{digest}'
    marker = directory / '.ai-memory.toml'
    try:
        with marker.open('x', encoding='utf-8') as file:
            workspace = os.environ.get("AI_MEMORY_WORKSPACE", "local")
            file.write("workspace = " + json.dumps(workspace) + "\nproject = " + json.dumps(name) + "\n")
    except FileExistsError:
        return scope(project)
    return {'workspace': os.environ.get('AI_MEMORY_WORKSPACE', 'local'), 'project': name, 'marker': str(marker), 'created': True}

def process_alive(pid):
    if not pid:
        return False
    if os.name == "nt":
        r = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"], capture_output=True, text=True, errors="replace", creationflags=subprocess.CREATE_NO_WINDOW)
        return r.returncode != 0 or f'","{pid}",' in r.stdout
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True

class Store:
    def __init__(self, root):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.root / "coordination.sqlite", timeout=20)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript("""
          CREATE TABLE IF NOT EXISTS tasks (
            id TEXT PRIMARY KEY, project TEXT NOT NULL, goal TEXT NOT NULL,
            status TEXT NOT NULL, revision INTEGER NOT NULL, snapshot TEXT NOT NULL,
            created TEXT NOT NULL, updated TEXT NOT NULL);
          CREATE TABLE IF NOT EXISTS runs (
            id TEXT PRIMARY KEY, task_id TEXT NOT NULL, category TEXT NOT NULL,
            status TEXT NOT NULL, client TEXT, model TEXT, started TEXT NOT NULL,
            ended TEXT, attempts TEXT NOT NULL, input_path TEXT NOT NULL,
            output_path TEXT, error TEXT, pid INTEGER, owner_pid INTEGER);
        """)
        columns = {r[1] for r in self.db.execute('PRAGMA table_info(runs)')}
        if 'usage' not in columns:
            try:
                self.db.execute('ALTER TABLE runs ADD COLUMN usage TEXT')
            except sqlite3.OperationalError as exc:
                if 'duplicate column' not in str(exc).lower():
                    raise
            self.db.commit()

    def task(self, ident):
        row = self.db.execute("SELECT * FROM tasks WHERE id=?", (validate_id(ident),)).fetchone()
        if not row:
            raise ValueError("Tarefa nao encontrada")
        value = dict(row)
        value["snapshot"] = json.loads(value["snapshot"])
        return value

    def mirror(self, value):
        atomic_json(self.root / "tasks" / value["id"] / "state.json", value)

    def new(self, project, goal):
        if not goal.strip() or len(goal) > 1500:
            raise ValueError("Objetivo deve ter 1 a 1500 caracteres")
        ident, stamp = uuid.uuid4().hex[:16], now()
        snapshot = {"summary": "", "decisions": [], "changed_files": [], "checks": [], "pending": [], "next_step": ""}
        with self.db:
            self.db.execute("INSERT INTO tasks VALUES (?,?,?,?,?,?,?,?)", (ident, project_path(project), scrub(goal), "active", 1, json.dumps(snapshot), stamp, stamp))
            self.mirror(self.task(ident))
        return self.task(ident)

    def context(self, project):
        rows = self.db.execute("SELECT id,goal,status,revision,updated FROM tasks WHERE project=? AND status='active' ORDER BY updated DESC LIMIT 8", (project_path(project),))
        return [dict(row) for row in rows]

    def checkpoint(self, ident, revision, patch, status=None):
        if not isinstance(patch, dict) or set(patch) - FIELDS:
            raise ValueError("Checkpoint possui campos desconhecidos")
        for key, value in patch.items():
            if key in ("summary", "next_step"):
                if not isinstance(value, str) or len(value) > 5000:
                    raise ValueError("Resumo/proximo passo: texto de ate 5000 caracteres")
            elif not isinstance(value, list) or len(value) > 40 or any(not isinstance(x, str) or len(x) > 1500 for x in value):
                raise ValueError("Listas: ate 40 textos curtos")
        self.db.execute("BEGIN IMMEDIATE")
        try:
            current = self.task(ident)
            if revision != current["revision"]:
                raise ValueError("Revisao mudou: releia resume antes de gravar")
            current["snapshot"].update(patch)
            snapshot = scrub(json.dumps(current["snapshot"], ensure_ascii=False))
            if len(snapshot) > 12000:
                raise ValueError("Checkpoint excede 12000 caracteres; resuma e referencie arquivos")
            self.db.execute("UPDATE tasks SET snapshot=?,revision=revision+1,status=?,updated=? WHERE id=?", (snapshot, status or current["status"], now(), ident))
            self.mirror(self.task(ident))
            self.db.commit()
        except BaseException:
            self.db.rollback()
            raise
        return self.task(ident)

    def runs(self, ident):
        self.task(ident)
        return [dict(row) for row in self.db.execute("SELECT * FROM runs WHERE task_id=? ORDER BY started DESC LIMIT 8", (ident,))]

    def reserve(self, ident, category, prompt, max_calls):
        if self.task(ident)["status"] != "active":
            raise ValueError("Tarefa encerrada")
        self.db.execute("BEGIN IMMEDIATE")
        try:
            if self.db.execute("SELECT 1 FROM runs WHERE task_id=? AND status='running'", (ident,)).fetchone():
                raise ValueError("Worker em execucao; consulte resume antes de repetir")
            used = sum(len(json.loads(row[0])) for row in self.db.execute("SELECT attempts FROM runs WHERE task_id=?", (ident,)))
            if used >= max_calls:
                raise ValueError("Limite de chamadas da tarefa atingido; reveja a tarefa, nao repita em loop")
            run_id = uuid.uuid4().hex[:16]
            folder = self.root / "tasks" / ident / "runs" / run_id
            folder.mkdir(parents=True)
            inp = folder / "input.md"
            inp.write_text(prompt, encoding="utf-8")
            self.db.execute("INSERT INTO runs(id,task_id,category,status,started,attempts,input_path,owner_pid) VALUES (?,?,?,?,?,?,?,?)", (run_id, ident, category, "running", now(), "[]", str(inp), os.getpid()))
            self.db.commit()
            return run_id, folder, max_calls - used
        except BaseException:
            self.db.rollback()
            raise

    def update_run(self, ident, **values):
        if not values or set(values) - {"status", "client", "model", "ended", "attempts", "output_path", "error", "pid", "usage"}:
            raise ValueError("Atualizacao de worker invalida")
        keys = list(values)
        with self.db:
            self.db.execute("UPDATE runs SET " + ",".join(k + "=?" for k in keys) + " WHERE id=?", [values[k] for k in keys] + [ident])

    def recover(self, task, run):
        self.task(task)
        row = self.db.execute("SELECT * FROM runs WHERE id=? AND task_id=?", (validate_id(run), task)).fetchone()
        if not row or row["status"] != "running":
            raise ValueError("Execucao pendente nao encontrada")
        if process_alive(row["owner_pid"]) or process_alive(row["pid"]):
            raise ValueError("Processo ainda existe; nao recuperar nem repetir")
        with self.db:
            self.db.execute("UPDATE runs SET status='interrupted',ended=?,error='Processos ausentes; interrupcao confirmada',pid=NULL WHERE id=? AND status='running'", (now(), run))
        return {"run": run, "status": "interrupted", "next_step": "Leia logs antes de decidir se precisa repetir"}

def launcher(config, client):
    command = runtime.launcher_command(config, client)
    problem = runtime.launcher_problem(command)
    if problem:
        raise ValueError(f"{problem} ({client})")
    return list(command)

def worker_args(config, candidate, final_path, prompt):
    client, model = candidate["client"], candidate["model"]
    cmd = launcher(config, client)
    if client == "codex":
        cmd += ["exec", "--model", model, "--sandbox", "read-only", "--skip-git-repo-check", "--ephemeral", "--color", "never", "-c", 'approval_policy="never"', "-c", 'mcp_servers.ai-memory.enabled=false', "-c", "features.memories=false", "-c", "project_doc_max_bytes=0", "-c", f'model_reasoning_effort="{candidate.get("effort", "low")}"', "--output-last-message", str(final_path), "-"]
        return cmd, prompt
    if client in ("openrouter", "chatgptweb", "geminiweb"):
        return cmd + ["--model", model, "--max-tokens", str(candidate.get("max_tokens", 1400))], prompt
    if client == "claude":
        return cmd + ["--print", "--model", model, "--effort", candidate.get("effort", "medium"), "--tools", "", "--strict-mcp-config", "--mcp-config", '{"mcpServers":{}}', "--output-format", "json"], prompt
    if client == "opencode":
        if int(config.get('availability', {}).get('opencode_cli_major', 1)) >= 2:
            return cmd + ["run", "--standalone", "--model", model, "--agent", "text-worker", "--title", "Worker " + model, "--format", "json", prompt], None
        return cmd + ["run", "--pure", "--model", model, "--agent", "text-worker", "--title", "Worker " + model, "--format", "json", prompt], None
    if client == "agy":
        return cmd + ["--print", prompt, "--model", model, "--mode", "plan", "--sandbox", "--output-format", "text", "--disable-slash-commands"], None
    raise ValueError("Cliente desconhecido")

def response_text(client, stdout, final_path):
    if client == "codex":
        return final_path.read_text(encoding="utf-8") if final_path.exists() else ""
    if client == "agy":
        return stdout.strip()
    if client in ("claude", "openrouter", "chatgptweb", "geminiweb"):
        try:
            obj = json.loads(stdout)
        except json.JSONDecodeError:
            return ""
        return "" if obj.get("is_error") or obj.get("error") else obj.get("result", "")
    texts = []
    for line in stdout.splitlines():
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if obj.get("type") == "error":
            return ""
        if obj.get("type") == "text" and isinstance(obj.get("part"), dict):
            texts.append(obj["part"].get("text", ""))
    return "\n".join(texts).strip()

def terminate_tree(proc):
    if os.name == "nt":
        subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"], capture_output=True, creationflags=subprocess.CREATE_NO_WINDOW)
    else:
        proc.kill()

def run_worker(store, config, ident, category, input_file, max_attempts=None):
    if os.environ.get("AI_COORDINATOR_ROLE") == "worker":
        raise ValueError("Worker nao pode delegar novamente")
    limits = config["limits"]
    body = Path(input_file).read_text(encoding="utf-8-sig")
    if not body.strip() or len(body) > limits["input_chars"]:
        raise ValueError(f"Entrada deve ter 1 a {limits['input_chars']} caracteres")
    plain = scrub(body)
    prompt = ("Worker textual: sem ferramentas, arquivos ou delegacao. Use somente o material fornecido. Respeite o formato e o limite solicitados; registre lacunas apenas quando compativel com o formato. O coordenador revisa e aplica.\n\n" + plain)
    run_id, folder, remaining = store.reserve(ident, category, prompt, limits["worker_calls_per_task"])
    attempts, result, skipped, metadata = [], "", [], {}
    health = Health(store.db)
    env = os.environ.copy()
    env.update(AI_COORDINATOR_ROLE="worker", AI_COORDINATOR_TASK_ID=ident, OPENCODE_DISABLE_CLAUDE_CODE="true")
    if int(config.get('availability', {}).get('opencode_cli_major', 1)) >= 2:
        env['OPENCODE_DISABLE_DEFAULT_PLUGINS'] = 'true'
    disabled = {name: {"enabled": False} for name in config.get("worker_mcp_disable_names", ["ai-memory"])}
    available = [candidate for candidate in config["routes"][category]["candidates"] if candidate["client"] not in config.get("disabled_worker_clients", [])]
    candidates = available
    attempt_limit = min(max_attempts or limits["attempts_per_run"], limits["attempts_per_run"], remaining)
    if not candidates:
        store.update_run(run_id, status="failed", ended=now(), error="Nenhum candidato habilitado para esta rota")
        return {"task_id": ident, "run_id": run_id, "status": "failed", "attempts": [], "next_step": "Verifique disponibilidade dos clientes; nao remova bloqueios sem validar"}
    try:
        for index, candidate in enumerate(candidates):
            if len(attempts) >= attempt_limit:
                break
            problem = runtime.launcher_problem(runtime.launcher_command(config, candidate['client']))
            if problem:
                skipped.append({'client': candidate['client'], 'model': candidate['model'],
                                'reason': {'state': 'launcher_missing', 'detail': problem}})
                continue
            if config.get('availability', {}).get('enabled', True):
                runtime.refresh_catalog(health, config, candidate)
                eligible, reason = health.eligible(candidate)
                if not eligible:
                    skipped.append({'client': candidate['client'], 'model': candidate['model'], 'reason': reason})
                    continue
            if candidate['client'] == 'opencode':
                env['OPENCODE_CONFIG_CONTENT'] = json.dumps({'permission': {'*': 'deny'}, 'mcp': disabled,
                    'small_model': candidate['model'], 'agent': {'text-worker': {'description': 'Worker textual delimitado', 'mode': 'primary', 'steps': 2, 'prompt': 'Responda apenas ao material fornecido, no formato solicitado. Sem ferramentas, arquivos ou delegacao.', 'permission': {'*': 'deny'}}}})
                env['OPENCODE_EXPERIMENTAL_OUTPUT_TOKEN_MAX'] = str(candidate.get('max_tokens', limits.get('native_output_tokens', 4096)))
            attempts.append({**candidate, "started": now(), "status": "running"})
            store.update_run(run_id, attempts=json.dumps(attempts), client=candidate["client"], model=candidate["model"])
            final, proc = folder / f"final-{index}.md", None
            try:
                # plain_prompt: interface web, onde o mesmo cabecalho fixo em toda conversa denuncia automacao.
                cmd, stdin = worker_args(config, candidate, final, plain if candidate.get("plain_prompt") else prompt)
                proc = subprocess.Popen(cmd, stdin=subprocess.PIPE if stdin is not None else subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8", errors="replace", cwd=folder, env=env, creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
                store.update_run(run_id, pid=proc.pid)
                try:
                    # Candidato lento (ex.: Gemini web) pode pedir prazo proprio, com teto de 10 min.
                    stdout, stderr = proc.communicate(stdin, timeout=min(600, candidate.get("timeout_seconds", limits["timeout_seconds"])))
                except subprocess.TimeoutExpired:
                    terminate_tree(proc)
                    stdout, stderr = proc.communicate()
                    (folder / f"stdout-{index}.log").write_text(scrub(stdout), encoding="utf-8")
                    (folder / f"stderr-{index}.log").write_text(scrub(stderr), encoding="utf-8")
                    raise TimeoutError("Tempo limite atingido; nao repetir automaticamente")
                (folder / f"stdout-{index}.log").write_text(scrub(stdout), encoding="utf-8")
                (folder / f"stderr-{index}.log").write_text(scrub(stderr), encoding="utf-8")
                result = response_text(candidate["client"], stdout, final) if proc.returncode == 0 else ""
                metadata = runtime.usage(candidate['client'], stdout)
                attempts[-1]['usage'] = metadata
                store.update_run(run_id, usage=json.dumps(metadata))
                if not isinstance(result, str) or not result.strip():
                    message, retry_after = runtime.safe_message(stdout, stderr, proc.returncode)
                    health.record(candidate['client'], candidate['model'], False, scrub(message), retry_after=retry_after)
                    raise RuntimeError(message)
                result = runtime.validate_output(result, candidate, metadata)
                health.record(candidate['client'], candidate['model'], True)
                attempts[-1].update(status="complete", ended=now())
                break
            except (OSError, ValueError, RuntimeError, TimeoutError) as exc:
                attempts[-1].update(status="failed", error=scrub(str(exc))[:1500], ended=now())
                if isinstance(exc, (OSError, ValueError, TimeoutError)):
                    health.record(candidate['client'], candidate['model'], False, scrub(str(exc)))
                result = ""
            finally:
                if proc is not None and proc.poll() is None:
                    terminate_tree(proc)
                store.update_run(run_id, attempts=json.dumps(attempts), pid=None)
        if result:
            metadata=runtime.aggregate_usage(attempts)
            output = folder / "result.md"
            output.write_text(scrub(result), encoding="utf-8")
            store.update_run(run_id, status="complete", ended=now(), output_path=str(output), usage=json.dumps(metadata))
            return {"task_id": ident, "run_id": run_id, "status": "complete", "client": attempts[-1]["client"], "model": attempts[-1]["model"], "output_file": str(output), "preview": scrub(result)[:limits["output_chars"]], "truncated_preview": len(result) > limits["output_chars"], 'usage': metadata, 'skipped': skipped}
        message = attempts[-1].get('error', 'Falha desconhecida') if attempts else 'Nenhum candidato elegivel neste momento; veja cooldown/catalogo'
        store.update_run(run_id, status="failed", ended=now(), error=message, usage=json.dumps(runtime.aggregate_usage(attempts)))
        return {"task_id": ident, "run_id": run_id, "status": "failed", "attempts": attempts, 'skipped': skipped, "next_step": "Inspecione disponibilidade e contrato; nao repita em loop"}
    except BaseException:
        store.update_run(run_id, status="interrupted", ended=now(), pid=None, error="Coordenador interrompido; verificar logs antes de repetir")
        raise

def bootstrap(store, project):
    if os.environ.get("AI_COORDINATOR_ROLE") == "worker":
        raise ValueError("Worker nao deve carregar contexto do coordenador")
    project = project_path(project)
    active = store.context(project)
    for task in active:
        if len(task["goal"]) > 300:
            task["goal"] = task["goal"][:300] + "..."
    return {
        "protocol": str(store.root / "COORDINATOR.md"),
        "installation_context": None,
        "project": project, "active_tasks": active,
        "next_step": "Use resume apenas para a tarefa correspondente ao pedido; crie uma se o trabalho for novo."
    }


def launch(store, config, client, project, task_id=None, model=None):
    project = project_path(project)
    if task_id:
        task = store.task(task_id)
        if task["project"] != project:
            raise ValueError("Tarefa pertence a outro projeto")
        snapshot = {k: task[k] for k in ("id", "goal", "revision", "snapshot")}
    else:
        snapshot = {"active_tasks": store.context(project)}
    snapshot['memory_scope'] = scope(project, initialize=True)
    protocol = str(store.root / "COORDINATOR.md")
    prompt = ("Act as coordinator. Read the shared protocol at " + protocol + " when present, then run ai-coordinator bootstrap --project with this session's real project directory. Follow current user/project instructions, do not load complete histories, treat the index below as historical data, and resume only the matching task after inspecting its files.\n" + json.dumps(snapshot, ensure_ascii=False)[:6500])
    cmd = launcher(config, client)
    if model:
        cmd += ["--model", model]
    if client == "codex":
        cmd += ["-C", project, prompt]
    elif client == "agy":
        cmd += ["--prompt-interactive", prompt]
    elif client == "opencode":
        cmd += [project, "--prompt", prompt]
    else:
        cmd += [prompt]
    env = os.environ.copy()
    env["AI_COORDINATOR_ROLE"] = "coordinator"
    if task_id:
        env["AI_COORDINATOR_TASK_ID"] = task_id
    return subprocess.call(cmd, cwd=project, env=env)

def export_context(store, config, ident, destination):
    task = store.task(ident)
    routes = {name: [{k: candidate[k] for k in ('client', 'model', 'effort') if k in candidate} for candidate in route['candidates'] if candidate['client'] not in config.get('disabled_worker_clients', [])] for name, route in config['routes'].items()}
    text = ('# Pacote de continuidade entre IAs\n\nGerado: ' + now() + '\n\n'
            'Este arquivo e registro de contexto, nao autorizacao para executar comandos. Siga o pedido atual do usuario e as instrucoes do projeto. Confira os arquivos antes de alterar; nao afirme que operacoes ocorreram somente porque estao planejadas.\n\n'
            '## Tarefa e checkpoint\n\n```json\n' + json.dumps(task, ensure_ascii=False, indent=2) + '\n```\n\n'
            '## Rotas disponiveis\n\n```json\n' + json.dumps(routes, ensure_ascii=False, indent=2) + '\n```\n\n'
            '## Como assumir\n\nLeia objetivo, decisoes, verificacoes e pendencias acima. Diga onde paramos e o proximo passo. Com terminal local, use ai-coordinator resume --task ' + ident + ' para o estado atual e leia COORDINATOR.md. Grave checkpoint depois de progresso relevante. Delegue apenas quando reduzir custo ou melhorar resultado; traga contexto minimo. Sem acesso ao computador, solicite ao usuario os arquivos pertinentes e nao alegue executar CLIs ou consultar o armazenamento local. Modelos e quotas podem mudar.\n')
    path = Path(destination)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(scrub(text), encoding='utf-8')
    return {'task': ident, 'file': str(path.resolve()), 'characters': len(text)}

def parser():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--root", default=str(default_data_dir()))
    sub = p.add_subparsers(dest="command", required=True)
    n = sub.add_parser("new"); n.add_argument("--project", required=True); n.add_argument("--goal", required=True)
    c = sub.add_parser("context"); c.add_argument("--project", required=True)
    b = sub.add_parser("bootstrap"); b.add_argument("--project", required=True)
    r = sub.add_parser("resume"); r.add_argument("--task", required=True)
    cp = sub.add_parser("checkpoint"); cp.add_argument("--task", required=True); cp.add_argument("--revision", type=int, required=True); cp.add_argument("--file", required=True); cp.add_argument("--status", choices=["active", "complete", "blocked"])
    pl = sub.add_parser("plan"); pl.add_argument("--category", required=True); pl.add_argument('--refresh', action='store_true')
    av = sub.add_parser('availability'); av.add_argument('--refresh', action='store_true')
    w = sub.add_parser("run"); w.add_argument("--task", required=True); w.add_argument("--category", required=True); w.add_argument("--input", required=True); w.add_argument("--max-attempts", type=int, choices=[1, 2])
    w.add_argument('--response-format', choices=['text','json','python','code'])
    w.add_argument('--exclude-client', action='append', default=[])
    rc = sub.add_parser("recover"); rc.add_argument("--task", required=True); rc.add_argument("--run", required=True)
    la = sub.add_parser("launch"); la.add_argument("--client", required=True, choices=["codex", "claude", "opencode", "agy"]); la.add_argument("--project", required=True); la.add_argument("--task"); la.add_argument("--model")
    sub.add_parser("routes")
    sc = sub.add_parser('scope'); sc.add_argument('--project', required=True); sc.add_argument('--init', action='store_true')
    ex = sub.add_parser('export'); ex.add_argument('--task', required=True); ex.add_argument('--to', required=True)
    return p

def main():
    args = parser().parse_args()
    root = initialize_data_dir(args.root)
    config_path = root / "routes.json"
    config = read_json(config_path)
    if args.command in ("plan", "run") and args.category not in config["routes"]:
        raise ValueError("Categoria desconhecida; consulte routes")
    if args.command == "routes":
        emit(config["routes"]); return 0
    if args.command == 'scope':
        emit(scope(args.project, args.init)); return 0
    store = Store(root)
    try:
        if args.command == 'plan':
            emit(runtime.route_plan(store,config,args.category,args.refresh))
        elif args.command == 'availability':
            health=Health(store.db)
            discoveries=[]; seen=set()
            for route in config['routes'].values():
                for candidate in route['candidates']:
                    key=(candidate['client'],runtime.provider_of(candidate['client'],candidate['model']))
                    if key in seen: continue
                    seen.add(key)
                    if args.refresh:
                        discoveries.append({'client':key[0],'provider':key[1],**runtime.refresh_catalog(health,config,candidate,force=True)})
            catalogs=[]
            for row in store.db.execute('SELECT client,provider,ids_json,observed,expires,error FROM catalogs'):
                item=dict(row); ids=json.loads(item.pop('ids_json'))
                item['model_count']=len(ids); item['free_models']=[m for m in ids if m.endswith(':free') or m.endswith('-free')][:50]
                catalogs.append(item)
            emit({'health':health.snapshot(),'catalogs':catalogs,'discovery':discoveries,'note':'Sem chamadas de IA para sondagem. Catalogo nao confirma saldo/cota; acesso confirmado somente pelo trabalho util.'})
        elif args.command == "new":
            emit(store.new(args.project, args.goal))
        elif args.command == "context":
            emit({"project": project_path(args.project), "active_tasks": store.context(args.project)})
        elif args.command == "bootstrap":
            emit(bootstrap(store, args.project))
        elif args.command == "resume":
            emit({"task": store.task(args.task), "recent_runs": store.runs(args.task)})
        elif args.command == "checkpoint":
            emit(store.checkpoint(args.task, args.revision, read_json(args.file), args.status))
        elif args.command == "recover":
            emit(store.recover(args.task, args.run))
        elif args.command == 'export':
            emit(export_context(store, config, args.task, args.to))
        elif args.command == "run":
            config['disabled_worker_clients']=list(set(config.get('disabled_worker_clients',[])+args.exclude_client))
            if args.response_format:
                for candidate in config['routes'][args.category]['candidates']:
                    candidate['response_format']=args.response_format
            result = run_worker(store, config, args.task, args.category, args.input, args.max_attempts)
            emit(result)
            return 0 if result["status"] == "complete" else 2
        elif args.command == "launch":
            return launch(store, config, args.client, args.project, args.task, args.model)
        return 0
    finally:
        store.db.close()

if __name__ == "__main__":
    try:
        sys.exit(main())
    except (ValueError, OSError, sqlite3.Error, KeyError) as exc:
        emit({"error": scrub(str(exc))}); sys.exit(2)
