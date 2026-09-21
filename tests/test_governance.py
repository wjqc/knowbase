"""隔离仓库回归：source 导入分层、待审隔离、来源幂等、历史保留、注入边界、HTML 转义。"""
import json
import os
import sys
import tempfile
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
tmp = Path(tempfile.mkdtemp(prefix='knowbase-governance-'))
os.environ['KNOWBASE_CONFIG'] = str(tmp/'config.json')
os.environ['KNOWBASE_REPO_PATH'] = str(tmp/'repo')
os.environ['KNOWBASE_AGENT_NAME'] = 'pytest-agent'
from knowbase import config, index, hooks, sources, store
from knowbase.__main__ import main
from knowbase.server import search_impl, read_impl, feedback_impl, save_impl
from knowbase.dashboard import generate
src=tmp/'source';src.mkdir()
(src/'a.md').write_text('# AlphaUnique 工程\n\nAlphaUnique 部署步骤',encoding='utf-8')
(src/'b.md').write_text('# BetaUnique 工程\n\nBetaUnique 部署步骤',encoding='utf-8')
# init --import-from 只产 source：缺 scope 拒绝；有 scope 生成 manifest，不产生知识卡
assert main(['init','--import-from',str(src)]) == 1
assert main(['init','--import-from',str(src),'--scope','demo']) == 0
assert len(list(sources.iter_manifests(config.repo_path()))) == 2
assert len(list(store.iter_all(config.repo_path(),True))) == 0
assert '未命中' in search_impl('AlphaUnique',scope='demo',include_inactive=True)
# 重复 init 导入：内容寻址幂等
assert main(['init','--import-from',str(src),'--scope','demo'])==0
assert len(list(sources.iter_manifests(config.repo_path())))==2
# 治理链改走 standard 提案卡（八项结构）
CARD=('## 结论\nAlphaUnique 工程部署须走灰度通道。\n\n## 解决的问题\n避免全量部署引发停机。\n\n'
      '## 适用条件\n- AlphaUnique 工程\n- 生产环境发布\n\n## 不适用条件\n- 本地联调\n\n'
      '## 可执行动作\n1. 提交灰度单\n2. 灰度小流量观察半小时\n\n## 关键证据\n发布检查单要求灰度记录。\n\n'
      '## 验证情况\n2026-09-21 验收环境灰度发布确认。\n\n## 未知与待确认\n多区发布策略未定。\n')
r=save_impl('standard','AlphaUnique 工程部署规范',CARD,scope='demo')
mid=r.split()[1]
assert '未命中' in search_impl('AlphaUnique',scope='demo')
assert mid in search_impl('AlphaUnique',scope='demo',include_inactive=True)
assert main(['promote',mid])==0
assert hooks.user_prompt('AlphaUnique 工程部署')==''  # once 未验证不注入（默认 verified 开关）
assert main(['verify',mid])==0
os.environ['CLAUDE_PROJECT_DIR']='/tmp/unrelated'
assert '未配置 scope_map' in hooks.user_prompt('AlphaUnique 工程部署')
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
