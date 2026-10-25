"""One-time explicit setup; never enables a model merely because it was discovered."""
import json
import re
from pathlib import Path

from .. import config, integration
from . import receipts

FIELDS = {'kind','executor_id','command','noninteractive_argv','version_arg','timeout_seconds',
          'auth_check','endpoint','protocol','allowed_hosts','allow_insecure_http','ca_bundle',
          'model','secret_ref','auth_kind','usage_source','bounded_tools','structured_output',
          'mcp_v2','max_output_tokens'}


def read_options(path):
    data=json.loads(Path(path).expanduser().read_text())
    if not isinstance(data,dict) or set(data)-FIELDS or not re.fullmatch(r'[A-Za-z0-9_.-]{1,64}',str(data.get('executor_id',''))):
        raise ValueError('invalid_executor_configuration')
    if any(isinstance(arg,str) and any(arg==flag or arg.startswith(flag+'=') for flag in
           ('--api-key','--password','--token','--secret')) for arg in data.get('noninteractive_argv',[])):
        raise ValueError('credential_arguments_forbidden')
    auth=data.get('auth_check',{})
    if not isinstance(auth,dict) or set(auth)-{'env_var','file'}:
        raise ValueError('invalid_auth_reference')
    if data.get('kind','command')=='intranet_text':
        from .text import IntranetTextRunner
        IntranetTextRunner(data)  # transport construction only, never a request
    elif data.get('kind','command')!='command' or not data.get('command'):
        raise ValueError('invalid_executor_configuration')
    return data


def _save(cfg):
    path=config.CONFIG_PATH.resolve()
    integration.apply([{'path':str(path),'before':path.read_bytes(),
                        'after':(json.dumps(cfg,ensure_ascii=False,indent=2)+'\n').encode()}],allowed_paths=(path,))


def configure(path):
    options=read_options(path);cfg=config.load_config();policy=cfg['automation']
    policy['executors']=[c for c in policy['executors'] if c.get('executor_id')!=options['executor_id']]+[options]
    _save(cfg)
    return {'configured':options['executor_id'],'enabled':bool(policy.get('enabled')),
            'verified':receipts.verified(options)}


def enable(executor_id, scopes, *, trust_command=False):
    cfg=config.load_config();policy=cfg['automation']
    options=next((c for c in policy['executors'] if c.get('executor_id')==executor_id),None)
    if not options or not receipts.verified(options):raise ValueError('verified_executor_required')
    if not scopes or not all(isinstance(s,str) and s.strip() and s!='*' for s in scopes):
        raise ValueError('explicit_scopes_required')
    if options.get('kind','command')=='command' and not trust_command:
        raise ValueError('explicit_command_trust_required')
    policy.update(enabled=True,allowed_scopes=list(dict.fromkeys(s.strip() for s in scopes)),
                  selected_executor=executor_id,allow_trusted_commands=bool(trust_command))
    _save(cfg);return {'enabled':True,'executor_id':executor_id,'scopes':policy['allowed_scopes']}


def disable():
    cfg=config.load_config();cfg['automation']['enabled']=False;_save(cfg)
    return {'enabled':False,'queued_material_preserved':True}
