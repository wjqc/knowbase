"""Optional intranet model intake. No embedding traffic without an explicit probe/build."""
import json
import time
from pathlib import Path
from urllib.parse import urlsplit

from . import config,integration
from .embedding_provider import OpenAICompatProvider,ProviderError


class ModelOptions(dict):
    secret_change=None  # In-memory bytes go through the same config journal, never JSON output.


def interactive():
    root=input('公司内网 Embedding API root（回车跳过）： ').strip()
    if not root:return None
    model=input('模型标识（可先留空，再用 --list-embedding-models 发现）： ').strip()
    env=input('认证环境变量名（默认 KNOWBASE_EMBED_API_KEY，不输入密钥）： ').strip() or 'KNOWBASE_EMBED_API_KEY'
    deployment=input('固定部署标识（可留空；未确认稳定身份时保持本地）： ').strip() or None
    scopes=input('允许发送到此内网服务的知识 scope（逗号分隔；回车不发送知识卡）： ').strip()
    insecure=False
    if urlsplit(root).scheme=='http':insecure=input('此地址使用HTTP。已获公司允许在此内网地址使用明文传输？[y/N] ').lower()=='y'
    if not model:
        discovery={'api_root':root,'model':'__discovery__','allowed_hosts':[urlsplit(root).hostname],
                   'api_key_env':env,'auth_kind':'bearer','allow_insecure_http':insecure}
        try:print('网关模型：'+', '.join(OpenAICompatProvider(discovery).models(deadline=time.monotonic()+3)))
        except (ProviderError,ValueError,TypeError):print('未能发现模型，可手填或回车跳过配置。')
        model=input('模型标识： ').strip()
        if not model:return None
    options=ModelOptions({'enabled':True,'api_root':root,'model':model,'allow_insecure_http':insecure,'allowed_hosts':[urlsplit(root).hostname],
            'api_key_env':env,'deployment_id':deployment,'allowed_scopes':[s.strip() for s in scopes.split(',') if s.strip()],
            'auth_kind':'bearer','auto_index':True})
    if input('GUI宿主需独立读取凭据；使用本机0600私有凭据文件？[y/N] ').lower()=='y':
        import getpass,uuid
        secret=getpass.getpass('Embedding 凭据（不会显示或写入配置JSON）： ')
        if not secret or len(secret.encode())>16384 or '\n' in secret or '\r' in secret:raise integration.IntegrationError('INVALID_SECRET_INPUT')
        path=Path.home()/'.knowbase/secrets'/('embedding-'+uuid.uuid4().hex)
        options['secret_ref']='file:'+str(path)
        options.secret_change={'path':str(path),'before':None,'after':secret.encode()}
    return options


def read_options(path):
    p=Path(path).expanduser()
    if p.stat().st_size>65536:raise integration.IntegrationError('MODEL_CONFIG_TOO_LARGE')
    try:return json.loads(p.read_text(encoding='utf-8'))
    except (ValueError, KeyError, TypeError):raise integration.IntegrationError('INVALID_MODEL_JSON') from None


def inspect_connection(*, models=False):
    from .semantic_index import settings
    try:
        p=OpenAICompatProvider(settings())
        if models:
            try:return {'models':p.models(deadline=time.monotonic()+3),'source':'gateway','index_ready':False}
            except ProviderError as exc:
                return {'models':[],'reason':exc.category,'action':'网关可能不支持模型列表，可手填模型再验证'}
        vectors,identity=p.embed(['knowbase synthetic connection probe'],deadline=time.monotonic()+3)
        return {'connection_verified':True,'identity':identity,'index_ready':False,'quality_approved':False,
                'action':'连接通过不代表索引、质量或Agent会话已生效'}
    except (ProviderError,ValueError,TypeError) as exc:
        return {'connection_verified':False,'reason':exc.category if isinstance(exc,ProviderError) else 'invalid_config',
                'base_retrieval_available':True}
