"""Low-cost catalog discovery and bounded, sanitized worker contracts."""
import ast
import datetime as dt
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import time
from .availability import Health, provider_of

BASE_WORKER_ENV = {
    "PATH", "HOME", "USER", "LOGNAME", "SHELL", "TMP", "TEMP", "TMPDIR",
    "LANG", "LC_ALL", "TERM", "COLORTERM", "XDG_CONFIG_HOME", "XDG_DATA_HOME",
    "XDG_CACHE_HOME", "XDG_RUNTIME_DIR", "USERPROFILE", "HOMEDRIVE", "HOMEPATH",
    "APPDATA", "LOCALAPPDATA", "PROGRAMDATA", "SYSTEMROOT", "WINDIR", "COMSPEC",
    "PATHEXT", "OS", "PROCESSOR_ARCHITECTURE", "NUMBER_OF_PROCESSORS",
}


def _environment_names(values, setting):
    if values is None:
        return set()
    if not isinstance(values, list) or any(
        not isinstance(value, str) or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", value)
        for value in values
    ):
        raise ValueError(f"worker_environment.{setting} deve conter nomes de variaveis validos")
    return {value.casefold() for value in values}


def worker_environment_policy(config):
    """Validate and summarize which host environment can reach a worker."""
    settings = config.get("worker_environment", {})
    if not isinstance(settings, dict):
        raise ValueError("worker_environment deve ser um objeto")
    mode = settings.get("mode", "allowlist")
    if not isinstance(mode, str) or mode not in {"allowlist", "inherit"}:
        raise ValueError("worker_environment.mode deve ser allowlist ou inherit")
    allowed = _environment_names(settings.get("allow", []), "allow")
    per_client = settings.get("allow_by_client", {})
    if not isinstance(per_client, dict) or any(not isinstance(client, str) for client in per_client):
        raise ValueError("worker_environment.allow_by_client deve mapear clientes para listas")
    clients = {}
    for client, names in per_client.items():
        clients[client] = sorted(_environment_names(names, f"allow_by_client.{client}"))
    return {
        "mode": mode,
        "inherits_parent_environment": mode == "inherit",
        "base_allowlist": sorted(allowed | {name.casefold() for name in BASE_WORKER_ENV}),
        "allow_by_client": clients,
    }


def worker_environment(config, candidate, task_id):
    """Build a worker environment with only platform essentials and named grants."""
    policy = worker_environment_policy(config)
    if policy["mode"] == "inherit":
        env = os.environ.copy()
    else:
        allowed = set(policy["base_allowlist"])
        allowed.update(policy["allow_by_client"].get(candidate["client"], []))
        env = {key: value for key, value in os.environ.items() if key.casefold() in allowed}
    env.update(
        AI_COORDINATOR_ROLE="worker",
        AI_COORDINATOR_TASK_ID=task_id,
        OPENCODE_DISABLE_CLAUDE_CODE="true",
    )
    if int(config.get("availability", {}).get("opencode_cli_major", 1)) >= 2:
        env["OPENCODE_DISABLE_DEFAULT_PLUGINS"] = "true"
    return env, policy["mode"]

def launcher_command(config, client, catalog=False):
    launchers=config.get('launchers',{})
    command=launchers.get(client,[]) or []
    if catalog:
        command=config.get('availability',{}).get('catalog_launchers',{}).get(client,command) or []
    return list(command)

def launcher_problem(command):
    if not isinstance(command,(list,tuple)) or not command:
        return 'Launcher nao configurado'
    executable=str(command[0])
    executable_path=Path(executable)
    if executable_path.is_absolute() or '/' in executable or '\\' in executable:
        exists=executable_path.is_file()
    else:
        exists=shutil.which(executable) is not None
    if not exists:
        return f'Executavel configurado ausente: {executable}'
    for index,arg in enumerate(command[:-1]):
        if str(arg).lower() in ('-file','--script'):
            script=Path(str(command[index+1]))
            if not script.is_file():
                return f'Script configurado ausente: {script}'
        elif Path(str(arg)).suffix.lower() in ('.py','.ps1') and ('/' in str(arg) or '\\' in str(arg)):
            script=Path(str(arg))
            if not script.is_file():
                return f'Script configurado ausente: {script}'
    return ''

def safe_message(stdout, stderr, returncode):
    retry_after=None
    for value in (stdout, stderr):
        for line in value.splitlines():
            try:
                event=json.loads(line)
            except ValueError:
                continue
            if not isinstance(event,dict):
                continue
            error=event.get('error')
            if error or event.get('is_error'):
                if isinstance(error,dict):
                    data=error.get('data') or {}
                    headers={str(k).lower():v for k,v in (data.get('responseHeaders') or {}).items()}
                    if str(headers.get('retry-after','')).isdigit():
                        retry_after=float(headers['retry-after'])
                    message=data.get('message') or error.get('message') or error.get('name')
                    status=data.get('statusCode')
                    if status:
                        message=f'HTTP {status}: {message}'
                else:
                    message=error or event.get('result')
                return str(message)[:700],retry_after
    first=(stderr or stdout).strip().splitlines()
    text=(first[0] if first else 'Sem resposta util')[:500]
    # Do not persist opaque headers/error bodies as a diagnosis.
    if 'responseHeaders' in text or 'set-cookie' in text.lower():
        text='Erro do provedor; consulte logs sanitizados da execucao.'
    return f'Worker exit={returncode}: {text}',retry_after

def usage(client, stdout):
    result={}
    for line in stdout.splitlines():
        try:
            obj=json.loads(line)
        except ValueError:
            continue
        if not isinstance(obj,dict):
            continue
        if client in ('claude','openrouter','chatgptweb','geminiweb'):
            if isinstance(obj.get('usage'),dict):
                result.update(obj['usage'])
            if obj.get('actual_model'):
                result['actual_model']=obj['actual_model']
            if obj.get('finish_reason'):
                result['finish_reason']=obj['finish_reason']
        elif client=='opencode' and obj.get('type')=='step_finish':
            part=obj.get('part') or {}
            tokens=part.get('tokens') or {}
            if isinstance(tokens.get('total'),(int,float)):
                result['total_tokens']=result.get('total_tokens',0)+tokens['total']
            if isinstance(part.get('cost'),(int,float)):
                result['cost']=result.get('cost',0)+part['cost']
            result['finish_reason']=part.get('reason')
    return result

def aggregate_usage(attempts):
    values=[a.get('usage') or {} for a in attempts]
    reported=sum(bool(v) for v in values)
    accounted=sum('total_tokens' in v and 'cost' in v for v in values)
    unreported=len(values)-reported
    incomplete=reported-accounted
    complete=bool(values) and accounted==len(values)
    status='complete' if complete else 'partial' if reported else 'unavailable'
    result={'reported_attempts':reported,'unreported_attempts':unreported,'incomplete_attempts':incomplete,
            'accounted_attempts':accounted,'attempts':len(attempts),'accounting_status':status,
            'complete_accounting':complete}
    for key in ('total_tokens','cost','input_tokens','output_tokens'):
        numbers=[v[key] for v in values if isinstance(v.get(key),(int,float))]
        if numbers: result[key]=sum(numbers)
    if values:
        for key in ('finish_reason','actual_model'):
            if key in values[-1]: result[key]=values[-1][key]
    return result

def validate_output(body, candidate, metadata):
    if metadata.get('finish_reason') in ('length','max_tokens'):
        raise ValueError('Invalid output: resposta truncada pelo limite de tokens')
    limit=candidate.get('max_output_chars')
    if limit is not None and len(body)>int(limit):
        raise ValueError('Invalid output: resposta excede o contrato de tamanho')
    fmt=candidate.get('response_format','text')
    if fmt=='text':
        return body.strip()
    outer=re.fullmatch(r'\s*```(?:[a-zA-Z0-9_-]+)?[ \t]*\r?\n(.*)\r?\n```\s*',body,re.S)
    fenced=re.findall(r'```(?:[a-zA-Z0-9_-]+)?\s*\r?\n(.*?)```',body,re.S)
    cleaned=outer.group(1).strip() if outer else fenced[0].strip() if len(fenced)==1 else body.strip()
    if fmt=='json':
        try:
            obj=json.loads(cleaned)
        except (ValueError,TypeError) as exc:
            raise ValueError('Invalid output: JSON invalido') from exc
        required=candidate.get('required_keys') or []
        if required and (not isinstance(obj,dict) or any(k not in obj for k in required)):
            raise ValueError('Invalid output: campos obrigatorios ausentes')
        return json.dumps(obj,ensure_ascii=False)
    if fmt=='python':
        try:
            ast.parse(cleaned)
        except SyntaxError as exc:
            raise ValueError('Invalid output: sintaxe Python invalida') from exc
    elif fmt!='code':
        raise ValueError('Formato de resposta desconhecido')
    return cleaned

def refresh_catalog(health, config, candidate, force=False):
    client,model=candidate['client'],candidate['model']
    provider=provider_of(client,model)
    policy=config.get('availability',{})
    if not policy.get('discover',False):
        return {'state':'not_requested'}
    ttl=int(policy.get('catalog_ttl_seconds',3600))
    row=health.conn.execute('SELECT observed,expires FROM catalogs WHERE client=? AND provider=?',(client,provider)).fetchone()
    if row and row[1]>time.time() and not force:
        return {'state':'cached'}
    # Failed discovery has its own retry window; do not repeat a metadata request per candidate.
    retry_key='catalog|'+client+'|'+provider
    health.conn.execute('CREATE TABLE IF NOT EXISTS catalog_retry (key TEXT PRIMARY KEY, retry_at REAL)')
    retry=health.conn.execute('SELECT retry_at FROM catalog_retry WHERE key=?',(retry_key,)).fetchone()
    if retry and retry[0]>time.time() and not force:
        return {'state':'discovery_cooldown'}
    now=time.time()
    try:
        if client=='codex':
            cache=Path(policy.get('codex_cache','~/.codex/models_cache.json')).expanduser()
            obj=json.loads(cache.read_text(encoding='utf-8'))
            models=obj.get('models') or []
            ids=[m.get('slug') for m in models if isinstance(m,dict) and isinstance(m.get('slug'),str)]
            observed=cache.stat().st_mtime
        elif client in ('opencode','agy'):
            # A metadata command must use the real native CLI, not a worker wrapper.
            command=launcher_command(config,client,catalog=True)
            problem=launcher_problem(command)
            if problem:
                health.catalog(client,provider,None,error=problem)
                return {'state':'launcher_missing','note':problem}
            if client=='opencode':
                if int(policy.get('opencode_cli_major',1))>=2:
                    command+=['models','--standalone']
                else:
                    command+=['models',provider,'--refresh']
            else:
                command+=['models']
            run=subprocess.run(command,capture_output=True,text=True,encoding='utf-8',errors='replace',timeout=20,creationflags=subprocess.CREATE_NO_WINDOW if os.name=='nt' else 0)
            if run.returncode!=0:
                raise ValueError('Falha no comando nativo de catalogo')
            if client=='opencode':
                ids=[line.strip() for line in run.stdout.splitlines() if line.strip().startswith(provider+'/') and not any(ch.isspace() for ch in line.strip())]
            else:
                ids=[line.split()[0] for line in run.stdout.splitlines() if re.match(r'^[a-z0-9][a-z0-9._-]+\s',line)]
            observed=now
        else:
            return {'state':'catalog_not_supported','note':'Disponibilidade verificada na chamada util; nao faz sondagem paga.'}
        if not health.catalog(client,provider,ids,at=observed,ttl=ttl):
            raise ValueError('Catalogo vazio/invalido; nao usado para excluir modelos')
        health.conn.execute('DELETE FROM catalog_retry WHERE key=?',(retry_key,))
        health.conn.commit()
        return {'state':'refreshed','count':len(ids),'observed':observed}
    except (OSError,ValueError,subprocess.TimeoutExpired) as exc:
        health.catalog(client,provider,None,error=str(exc))
        health.conn.execute('INSERT INTO catalog_retry VALUES (?,?) ON CONFLICT(key) DO UPDATE SET retry_at=excluded.retry_at',(retry_key,now+300))
        health.conn.commit()
        return {'state':'discovery_failed','note':str(exc)[:150]}

def route_plan(store,config,category,refresh=False):
    health=Health(store.db)
    route=dict(config['routes'][category])
    allowed=[]; skipped=[]
    for candidate in route['candidates']:
        if candidate['client'] in config.get('disabled_worker_clients',[]):
            skipped.append({**candidate,'reason':{'state':'operator_disabled'}})
            continue
        problem=launcher_problem(launcher_command(config,candidate['client']))
        if problem:
            skipped.append({**candidate,'availability':{'state':'launcher_missing','detail':problem}})
            continue
        if refresh:
            refresh_catalog(health,config,candidate)
        ok,reason=health.eligible(candidate)
        item={**candidate,'availability':reason}
        (allowed if ok else skipped).append(item)
    route.update(candidates=allowed,skipped=skipped,
                 worker_environment=worker_environment_policy(config),
                 availability_note='Catalogo/sucesso recente nao garantem cota. O trabalho util confirma disponibilidade e falhas alimentam cooldown.')
    return route
