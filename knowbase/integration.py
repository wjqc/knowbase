"""Host config merge with durable backups and CAS recovery. Never launches hosts."""
import hashlib
import json
import os
import shlex
import uuid
from contextlib import contextmanager
from pathlib import Path

HOSTS = {
    "cursor": {"mcp": ".cursor/mcp.json", "key": ("mcpServers",), "hooks": None, "style": None},
    "claude-code": {"mcp": ".claude.json", "key": ("mcpServers",), "hooks": ".claude/settings.json", "style": "claude"},
    "zcode": {"mcp": ".zcode/cli/config.json", "key": ("mcp", "servers"), "hooks": ".zcode/cli/config.json", "style": "zcode"},
    "trae": {"mcp": "Library/Application Support/Trae CN/User/mcp.json", "key": ("mcpServers",), "hooks": ".trae-cn/hooks.json", "style": "trae"},
}
EVENTS = {"SessionStart": "session-start", "UserPromptSubmit": "user-prompt"}


class IntegrationError(ValueError):
    pass


def _digest(raw):
    return hashlib.sha256(raw).hexdigest() if raw is not None else None


def _read(path):
    if path.is_symlink():
        raise IntegrationError("SYMLINK_CONFIG")
    if path.exists() and not path.is_file():
        raise IntegrationError("CONFIG_NOT_FILE")
    if path.exists() and path.stat().st_size > 4 * 1024 * 1024:
        raise IntegrationError("CONFIG_TOO_LARGE")
    return path.read_bytes() if path.exists() else None


def _document(raw):
    try:
        result = json.loads(raw) if raw is not None else {}
    except (ValueError, UnicodeError):
        raise IntegrationError("INVALID_HOST_JSON") from None
    if not isinstance(result, dict):
        raise IntegrationError("INVALID_HOST_OBJECT")
    return result


def _section(doc, keys):
    for key in keys:
        if key not in doc:
            doc[key] = {}
        if not isinstance(doc[key], dict):
            raise IntegrationError("HOST_SCHEMA_CONFLICT")
        doc = doc[key]
    return doc


def _same_command(actual,expected):
    if expected["type"]!="command":return actual.get("command")==expected["command"]
    command=actual.get("command")
    if not isinstance(command,str):return False
    try:return shlex.split(command)==shlex.split(expected["command"])
    except ValueError:return False


def runtime_command(home):
    return str(home / ".knowbase/runtime/venv/bin/python")


def discover(home=None):
    home = Path(home or Path.home()).resolve()
    result = []
    for name, host in HOSTS.items():
        p = home / host["mcp"]
        h = home / host["hooks"] if host["hooks"] else p
        if not p.exists() and not h.parent.exists() and not host_metadata(name,home)["installed"]:
            continue
        item = {"id": name, "configured": False, "session_effective": "unknown",
                "protocol_verified": False, "hook_capability": "version_probe_pending"}
        try:
            server = _section(_document(_read(p)), host["key"]).get("knowbase", {})
            item["configured"] = (isinstance(server, dict) and server.get("command") == runtime_command(home) and
                                  server.get("args") == ["-I", "-m", "knowbase", "serve"])
            hdoc = _document(_read(h))
            hooks = hdoc.get("hooks", {})
            if name == "zcode":
                item["hook_enabled"] = hooks.get("enabled", "unknown")
                hooks = hooks.get("events", {})
            item["hook_events"] = sorted(k for k in EVENTS if isinstance(hooks.get(k), list) and hooks[k])
            item["hooks_configured"] = len(item["hook_events"]) == len(EVENTS)
        except (ValueError, OSError, AttributeError):
            item["error"] = "invalid_configuration"
        item.update(host_metadata(name,home))
        if name=="cursor":item["hook_capability"]="mcp_only"
        if name=="trae":item["hook_action"]="核对原生开关和重复导入Claude Hook"
        result.append(item)
    # Discovery does not claim unimplemented host adapters are installable.
    for name, rel in (("codex", ".codex/config.toml"),
                      ("opencode", ".config/opencode/opencode.jsonc")):
        if (home / rel).exists() or host_metadata(name,home)["installed"]:
            path=home/rel
            item={"id":name,"configured":False,"adapter":"available","session_effective":"unknown","protocol_verified":False,
                  "hook_status":"trust_required" if name=="codex" else "mcp_only"}
            try:
                if name=="codex":
                    own=_toml(_read(path)).get("mcp_servers",{}).get("knowbase",{})
                    item["configured"]=own.get("command")==runtime_command(home) and own.get("args")==["-I","-m","knowbase","serve"]
                else:
                    own=_jsonc_tokens(_read(path))[2].get("mcp",{}).get("knowbase",{})
                    item["configured"]=own.get("command")==[runtime_command(home),"-I","-m","knowbase","serve"]
            except (ValueError,OSError,TypeError):item["error"]="invalid_configuration"
            item.update(host_metadata(name,home))
            result.append(item)
    return result


def plan(agents, home=None):
    home = Path(home or Path.home()).resolve()
    documents, originals = {}, {}
    def document(rel):
        path = home / rel
        # Reject symlink parents as well as a symlink leaf.
        if any(p.is_symlink() for p in [path] + list(path.parents) if p != home.parent):
            raise IntegrationError("SYMLINK_CONFIG")
        if path not in documents:
            raw = _read(path)
            originals[path] = raw
            documents[path] = _document(raw)
        return documents[path]
    extra=[name for name in agents if name in {"codex","opencode"}]
    for name in dict.fromkeys(agents):
        if name in extra:continue
        if name not in HOSTS:
            raise IntegrationError("UNSUPPORTED_HOST:" + name)
        host = HOSTS[name]
        server = {"command": runtime_command(home), "args": ["-I", "-m", "knowbase", "serve"],
                  "env": {"KNOWBASE_AGENT_NAME": name}}
        from . import config
        server["env"]["KNOWBASE_CONFIG"] = str(config.CONFIG_PATH.resolve())
        servers = _section(document(host["mcp"]), host["key"])
        existing = servers.get("knowbase")
        if existing and (not isinstance(existing, dict) or
                         existing.get("command") != server["command"] or existing.get("args") != server["args"]):
            raise IntegrationError("EXISTING_KNOWBASE_CONFLICT:" + name)
        current_env = (existing or {}).get("env", {})
        if not isinstance(current_env, dict) or any(k in current_env and current_env[k] != v for k,v in server["env"].items()):
            raise IntegrationError("EXISTING_KNOWBASE_ENV_CONFLICT:" + name)
        server["env"] = {**current_env, **server["env"]}
        servers["knowbase"] = {**(existing or {}), **server}
        if not host["hooks"]:continue
        hooks = _section(document(host["hooks"]), ("hooks", "events") if name == "zcode" else ("hooks",))
        events=dict(EVENTS)
        events.update({"PreToolUse":"tool-before","PostToolUse":"tool-after","Stop":"stop"})
        if name in {"claude-code","zcode"}:events["PostToolUseFailure"]="tool-failure"
        if name=="claude-code":events["PreCompact"]="before-compact"
        for event, action in events.items():
            command = f'{shlex.quote(runtime_command(home))} -I -m knowbase hook {action} --style {host["style"]}'
            handler = ({"type": "process", "command": runtime_command(home), "args": ["-I", "-m", "knowbase", "hook", action, "--style", host["style"]], "timeoutMs": 1000}
                       if name == "zcode" else {"type": "command", "command": command, "timeout": 1})
            entries = hooks.setdefault(event, [])
            if not isinstance(entries, list):
                raise IntegrationError("HOOK_SCHEMA_CONFLICT")
            handlers = [h for e in entries if isinstance(e, dict) for h in e.get("hooks", []) if isinstance(h, dict)]
            def own(h):
                return "knowbase" in str(h.get("command", "")) or "knowbase" in h.get("args", [])
            owned = [h for h in handlers if own(h)]
            if owned:
                for h in owned:
                    same_command=_same_command(h,handler)
                    if not same_command or any(h.get(k) != handler.get(k) for k in ("type", "args")):
                        raise IntegrationError("EXISTING_HOOK_CONFLICT:" + name + ":" + event)
                    # Preserve the existing timeout and matchers. No replacement of user handlers.
            if not owned:
                entries.append({"hooks": [handler]})
    return extra_plan(extra,home) + [{"path": str(path), "before": originals[path],
             "after": (json.dumps(doc, ensure_ascii=False, indent=2) + "\n").encode()}
            for path, doc in documents.items() if doc != _document(originals[path])]


def _atomic(path, raw):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temp = path.with_name(path.name + ".knowbase-" + uuid.uuid4().hex)
    try:
        with temp.open("xb") as f:
            os.chmod(temp, 0o600)
            f.write(raw); f.flush(); os.fsync(f.fileno())
        os.replace(temp, path)
        if os.name != "nt":
            fd = os.open(path.parent, os.O_RDONLY)
            try: os.fsync(fd)
            finally: os.close(fd)
    finally:
        temp.unlink(missing_ok=True)


@contextmanager
def _lock(home):
    from .locking import RepoLock
    directory = home / ".knowbase"
    if directory.is_symlink() or (directory / ".lock").is_symlink():
        raise IntegrationError("SYMLINK_LOCK")
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    # Reuse process-safe OS locks; crashes release them automatically.
    with RepoLock(directory, timeout=1):
        yield


def resume(home=None):
    home = Path(home or Path.home()).resolve()
    with _lock(home):
        for path in sorted((home / ".knowbase/backups").glob("init-*/journal.json")):
            journal = json.loads(path.read_text())
            if journal.get("state") in {"prepared", "rollback_conflict"}:
                if recover(path):
                    raise IntegrationError("ROLLBACK_CONFLICT")


def recover(journal_path):
    """Rollback only our bytes; preserve any concurrent user edit."""
    path = Path(journal_path); journal = json.loads(path.read_text())
    # Do not follow arbitrary paths injected into an untrusted journal.
    home = path.parents[3].resolve()
    for item in journal["files"]:
        target = Path(item["path"])
        from . import config
        if (not target.is_relative_to(home) and target != config.CONFIG_PATH.resolve()) or target.is_symlink():
            raise IntegrationError("INVALID_JOURNAL_TARGET")
        if item["backup"] and Path(item["backup"]).parent != path.parent:
            raise IntegrationError("INVALID_JOURNAL_BACKUP")
    conflicts = []
    for item in reversed(journal["files"]):
        target = Path(item["path"]); current = _read(target)
        if _digest(current) == item["before_hash"]:
            continue
        if _digest(current) != item["after_hash"]:
            conflicts.append(str(target)); continue
        if item["backup"]:
            raw = Path(item["backup"]).read_bytes()
            if _digest(raw) != item["before_hash"]:
                conflicts.append(str(target)); continue
            _atomic(target, raw)
            os.chmod(target, item["mode"])
        else:
            target.unlink()
    journal["state"] = "rollback_conflict" if conflicts else "rolled_back"
    _atomic(path, (json.dumps(journal, indent=2) + "\n").encode())
    return conflicts


def apply(changes, home=None, *, allowed_paths=()):
    home = Path(home or Path.home()).resolve()
    if not changes:
        return {"state": "unchanged", "configured": True, "session_effective": "reconnect_required"}
    with _lock(home):
        if (home / ".knowbase/backups").is_symlink():
            raise IntegrationError("SYMLINK_BACKUP_DIRECTORY")
        backup = home / ".knowbase/backups" / ("init-" + uuid.uuid4().hex)
        backup.mkdir(parents=True, mode=0o700)
        journal = {"state": "prepared", "files": []}
        for i, change in enumerate(changes):
            p = Path(change["path"])
            if (not p.is_relative_to(home) and p not in allowed_paths) or any(q.is_symlink() for q in [p] + list(p.parents)) :
                raise IntegrationError("CONFIG_OUTSIDE_HOME")
            if _read(p) != change["before"]:
                raise IntegrationError("CONCURRENT_CONFIG_CHANGE")
            saved = backup / str(i) if change["before"] is not None else None
            if saved:
                _atomic(saved, change["before"])
            journal["files"].append({"path": str(p), "before_hash": _digest(change["before"]),
                "after_hash": _digest(change["after"]), "backup": str(saved) if saved else None,
                "mode": p.stat().st_mode & 0o777 if p.exists() else 0o600})
        jp = backup / "journal.json"
        _atomic(jp, (json.dumps(journal, indent=2) + "\n").encode())
        try:
            for change, item in zip(changes, journal["files"]):
                p = Path(change["path"])
                if _read(p) != change["before"]:
                    raise IntegrationError("CONCURRENT_CONFIG_CHANGE")
                _atomic(p, change["after"])
                if _read(p) != change["after"]:
                    raise IntegrationError("CONFIG_READBACK_FAILED")
            journal["state"] = "configured"
            _atomic(jp, (json.dumps(journal, indent=2) + "\n").encode())
        except Exception:
            recover(jp)
            raise
    return {"state": "configured", "configured": True, "backup": str(backup),
            "protocol_verified": False, "session_effective": "reconnect_required"}


# Additional hosts keep their native formats; no shell callbacks or trust edits.
def _toml(raw):
    try:
        import tomllib
    except ImportError:
        try:import tomli as tomllib
        except ImportError:raise IntegrationError("TOML_PARSER_UNAVAILABLE") from None
    try:return tomllib.loads((raw or b'').decode())
    except (ValueError,UnicodeError):raise IntegrationError("INVALID_HOST_TOML") from None


def _jsonc_tokens(raw):
    import re
    text=(raw or b'{}').decode()
    pattern=re.compile(r'\s+|//[^\n]*|/\*[\s\S]*?\*/|"(?:\\.|[^"\\])*"|[{}\[\]:,]|[^\s{}\[\]:,/]+')
    tokens=[];pos=0
    for m in pattern.finditer(text):
        if text[pos:m.start()].strip():raise IntegrationError("INVALID_HOST_JSONC")
        token=m.group();pos=m.end()
        if token.isspace() or token.startswith(('//','/*')):continue
        tokens.append((token,m.start(),m.end()))
    if text[pos:].strip():raise IntegrationError("INVALID_HOST_JSONC")
    cleaned=''.join(t[0] for i,t in enumerate(tokens) if not (t[0]==',' and i+1<len(tokens) and tokens[i+1][0] in {'}',']'}))
    try:doc=json.loads(cleaned)
    except ValueError:raise IntegrationError("INVALID_HOST_JSONC") from None
    if not isinstance(doc,dict):raise IntegrationError("INVALID_HOST_OBJECT")
    return text,tokens,doc


def _jsonc_merge(raw,key,value):
    text,tokens,doc=_jsonc_tokens(raw)
    parent=doc.get('mcp')
    if parent is not None and not isinstance(parent,dict):raise IntegrationError("HOST_SCHEMA_CONFLICT")
    if parent is not None and key in parent:
        if parent[key]!=value:raise IntegrationError("EXISTING_KNOWBASE_CONFLICT:opencode")
        return raw
    if not tokens or tokens[0][0]!='{':raise IntegrationError("INVALID_HOST_JSONC")
    opening=0
    if parent is not None:
        depth=0
        for i,t in enumerate(tokens):
            if t[0] in {'{','['}:depth+=1
            elif t[0] in {'}',']'}:depth-=1
            if depth==1 and t[0]=='"mcp"' and tokens[i+1][0]==':' and tokens[i+2][0]=='{':
                opening=i+2;break
        else:raise IntegrationError("HOST_SCHEMA_CONFLICT")
    depth=0;closing=None
    for i in range(opening,len(tokens)):
        if tokens[i][0] in {'{','['}:depth+=1
        elif tokens[i][0] in {'}',']'}:depth-=1
        if depth==0:closing=i;break
    if closing is None:raise IntegrationError("INVALID_HOST_JSONC")
    addition=json.dumps(key if parent is not None else 'mcp')+': '+json.dumps(value if parent is not None else {key:value},ensure_ascii=False)
    previous=tokens[closing-1][0]
    comma='' if previous in {'{',','} else ','
    offset=tokens[closing][1]
    result=(text[:offset]+comma+'\n  '+addition+'\n'+text[offset:]).encode()
    _jsonc_tokens(result)
    return result


def extra_plan(agents,home):
    from . import config
    result=[]
    command=runtime_command(home);env={'KNOWBASE_AGENT_NAME':'','KNOWBASE_CONFIG':str(config.CONFIG_PATH.resolve())}
    for name in dict.fromkeys(agents):
        ownenv={**env,'KNOWBASE_AGENT_NAME':name}
        if name=='codex':
            path=home/'.codex/config.toml';before=_read(path);doc=_toml(before)
            server={'command':command,'args':['-I','-m','knowbase','serve'],'env':ownenv}
            if not isinstance(doc.get('hooks',{}),dict) or not isinstance(doc.get('mcp_servers',{}),dict):raise IntegrationError('HOST_SCHEMA_CONFLICT')
            if any('knowbase' in str(v) for k,v in doc.get('hooks',{}).items() if k in {'SessionStart','UserPromptSubmit','PreToolUse','PostToolUse','Stop','PreCompact'}):
                raise IntegrationError('DUPLICATE_INLINE_HOOK_SOURCE:codex')
            current=doc.get('mcp_servers',{}).get('knowbase')
            if current is not None:
                if current!=server:raise IntegrationError("EXISTING_KNOWBASE_CONFLICT:codex")
            else:
                text=(before or b'').decode()
                after=(text+'\n[mcp_servers.knowbase]\ncommand = '+json.dumps(command)+'\nargs = ["-I", "-m", "knowbase", "serve"]\n[mcp_servers.knowbase.env]\n'+''.join(k+' = '+json.dumps(v)+'\n' for k,v in ownenv.items())).encode()
                _toml(after);result.append({'path':str(path),'before':before,'after':after})
            hp=home/'.codex/hooks.json';hr=_read(hp);hd=_document(hr);hooks=_section(hd,('hooks',))
            for event,action in {**EVENTS,'PreToolUse':'tool-before','PostToolUse':'tool-after','Stop':'stop','PreCompact':'before-compact'}.items():
                handler={'type':'command','command':f'{shlex.quote(command)} -I -m knowbase hook {action} --style codex','timeout':1}
                entries=hooks.setdefault(event,[])
                if not isinstance(entries,list):raise IntegrationError('HOOK_SCHEMA_CONFLICT')
                owned=[h for entry in entries for h in entry.get('hooks',[]) if 'knowbase' in str(h.get('command',''))]
                if owned and not all(h.get('type')=='command' and _same_command(h,handler) for h in owned):raise IntegrationError('EXISTING_HOOK_CONFLICT:codex')
                if not owned:entries.append({'hooks':[handler]})
            if hd!=_document(hr):result.append({'path':str(hp),'before':hr,'after':(json.dumps(hd,ensure_ascii=False,indent=2)+'\n').encode()})
        elif name=='opencode':
            path=home/'.config/opencode/opencode.jsonc';before=_read(path)
            value={'type':'local','command':[command,'-I','-m','knowbase','serve'],'enabled':True,'environment':ownenv}
            after=_jsonc_merge(before,'knowbase',value)
            if after!=before:result.append({'path':str(path),'before':before,'after':after})
    return result


def model_change(options):
    """One non-secret model configuration change, handled by the same backup journal."""
    from . import config
    from .embedding_provider import OpenAICompatProvider,ProviderError
    allowed={'enabled','provider','api_root','base_url','model','allowed_hosts','allowed_scopes','api_key_env','auth_kind',
             'deployment_id','quality_report','query_prefix','document_prefix','chunk_chars','max_chunks','auto_index',
             'timeout_seconds','batch_timeout_seconds','allow_insecure_http','ca_bundle','dimensions','response_model','tenant','secret_ref'}
    if not isinstance(options,dict) or set(options)-allowed:raise IntegrationError('UNKNOWN_OR_SECRET_MODEL_FIELD')
    try:OpenAICompatProvider(options)
    except (ProviderError,ValueError,TypeError) as exc:
        raise IntegrationError('INVALID_MODEL_CONFIGURATION') from None
    path=config.CONFIG_PATH.resolve();before=_read(path);doc=_document(before)
    doc['embedding']=dict(options,enabled=True)
    return {'path':str(path),'before':before,'after':(json.dumps(doc,ensure_ascii=False,indent=2)+'\n').encode()}


async def probe_protocol(*, executable=None, args=None, cwd=None, timeout=10):
    """An isolated stdio probe, not proof that a host's real session loaded the build."""
    import asyncio,tempfile
    from mcp import ClientSession,StdioServerParameters
    from mcp.client.stdio import stdio_client
    from . import config
    async def run():
        with tempfile.TemporaryDirectory(prefix='knowbase-stdio-') as directory:
            root=Path(directory);rp=root/'memory';rp.mkdir()
            # No application CLI/init/sync needed to list tool capabilities.
            cp=root/'config.json';cp.write_text(json.dumps({'repo_path':str(rp),'git':{'auto_pull':False,'auto_push':False,'auto_commit':False},'embedding':{'enabled':False}}))
            params=StdioServerParameters(command=executable or runtime_command(Path.home()),args=args or ['-I','-m','knowbase','serve'],cwd=cwd,
                env={'KNOWBASE_CONFIG':str(cp),'KNOWBASE_REPO_PATH':str(rp),'KNOWBASE_AGENT_NAME':'integration-probe'})
            async with stdio_client(params) as (read,write):
                async with ClientSession(read,write) as client:
                    initialized=await client.initialize();tools=await client.list_tools()
                    required={'memory_search','memory_context','memory_use','memory_feedback'}
                    if not required<={t.name for t in tools.tools}:return {'protocol_verified':False,'reason':'tools_missing'}
                    from .build_identity import build_id
                    loaded='[knowbase-build:'+build_id()+']' in (initialized.instructions or '')
                    return {'protocol_verified':loaded,'reason':None if loaded else 'runtime_build_mismatch',
                            'session_effective':'reconnect_required','tools':len(tools.tools)}
    try:return await asyncio.wait_for(run(),timeout=timeout)
    except Exception:return {'protocol_verified':False,'reason':'stdio_probe_failed','session_effective':'unknown'}


def host_metadata(name,home=None):
    """Read installed app/CLI metadata without starting a host or changing a session."""
    import plistlib,re,shutil
    home=Path(home or Path.home()).resolve()
    apps={'codex':'Codex.app','cursor':'Cursor.app','trae':'Trae CN.app','zcode':'ZCode.app'}
    cli={'claude-code':'claude','zcode':'zcode','codex':'codex','cursor':'cursor','opencode':'opencode'}.get(name)
    executable=shutil.which(cli) if cli else None
    result={'installed':bool(executable),'version':None,'version_source':None,'native_event_verified':False}
    if executable:
        for part in reversed(Path(executable).resolve().parts):
            if re.fullmatch(r'v?\d+\.\d+\.\d+(?:[-+][A-Za-z0-9.]+)?',part):
                result.update(version=part,version_source='executable_path');break
    if name in apps:
        p=Path('/Applications')/apps[name]/'Contents/Info.plist'
        if p.exists():
            try:
                value=plistlib.loads(p.read_bytes()).get('CFBundleShortVersionString')
                result.update(installed=True,version=value,version_source='application_bundle')
            except (OSError,ValueError):pass
    return result
