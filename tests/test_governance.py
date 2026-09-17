"""隔离仓库回归：待审隔离、来源幂等、历史保留、注入边界、HTML 转义。"""
import json
import os
import sys
import tempfile
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
tmp = Path(tempfile.mkdtemp(prefix='knowbase-governance-'))
os.environ['KNOWBASE_CONFIG'] = str(tmp/'config.json')
os.environ['KNOWBASE_REPO_PATH'] = str(tmp/'repo')
from knowbase import config, index, hooks, store
from knowbase.__main__ import main
from knowbase.server import search_impl, read_impl, feedback_impl
from knowbase.dashboard import generate
src=tmp/'source';src.mkdir()
(src/'a.md').write_text('# AlphaUnique 工程\n\nAlphaUnique 部署步骤',encoding='utf-8')
(src/'b.md').write_text('# BetaUnique 工程\n\nBetaUnique 部署步骤',encoding='utf-8')
assert main(['init','--import-from',str(src)]) == 1
assert main(['init','--import-from',str(src),'--scope','demo']) == 0
mem=list(store.iter_all(config.repo_path(),True));assert len(mem)==2
assert len({m['id'] for m,_,_ in mem})==2
mid=mem[0][0]['id']
assert '未命中' in search_impl('AlphaUnique',scope='demo')
assert mid in search_impl('AlphaUnique',scope='demo',include_inactive=True)
assert main(['init','--import-from',str(src),'--scope','demo'])==0
assert len(list(store.iter_all(config.repo_path(),True)))==2
assert main(['promote',mid])==0
assert hooks.user_prompt('AlphaUnique 工程部署')==''
assert main(['verify',mid])==0
os.environ['CLAUDE_PROJECT_DIR']='/tmp/unrelated'
assert hooks.user_prompt('AlphaUnique 工程部署')==''
os.environ['CLAUDE_PROJECT_DIR']='/tmp/demo'
assert mid in hooks.user_prompt('AlphaUnique 工程部署')
read_impl(mid);feedback_impl(mid,'helpful')
conn=index.connect(config.repo_path());before=index.search_history(conn);conn.close()
assert main(['reindex'])==0
conn=index.connect(config.repo_path())
assert index.search_history(conn)==before
assert conn.execute('SELECT hit_count FROM meta WHERE id=?',(mid,)).fetchone()[0]==1
assert conn.execute('SELECT count(*) FROM feedback_log').fetchone()[0]==1
assert not index.search(conn,'%',scope='demo')
index.record_search(conn,'</script><img src=x onerror=alert(1)>','demo',[])
conn.close()
out=generate(config.repo_path(),tmp/'dashboard.html')
assert '</script><img' not in out.read_text()
feedback_impl(mid,'incorrect')
assert hooks.user_prompt('AlphaUnique 工程部署')==''
assert '未命中' in search_impl('AlphaUnique',scope='demo')
assert mid in search_impl('AlphaUnique',scope='demo',include_inactive=True)
print('Governance regression passed:',out)
