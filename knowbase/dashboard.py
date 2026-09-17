"""离线 HTML 治理快照；无外部资源，不执行知识正文中的 HTML。"""
import json
from collections import Counter
from datetime import datetime
from pathlib import Path

from . import index, store


def generate(repo: Path, output: Path) -> Path:
    conn = index.connect(repo)
    try:
        history = index.search_history(conn, 10000)
        hits = Counter(h['id'] for event in history for h in (event.get('hits') or []))
        items = []
        for row in conn.execute('SELECT * FROM meta'):
            m = index._row_meta(row)
            source, _, _ = store.load(repo, m['id'])
            relations = (source or {}).get('relations') or []
            reasons = []
            if m['status'] == 'stale':
                reasons.append('已标记失效 / 待复核')
            if m['unhelpful_count']:
                reasons.append('有负面反馈（不等于已证实错误）')
            if any(r.get('type') == 'contradicts' for r in relations):
                reasons.append('存在矛盾关系')
            items.append({**m, 'search_hits': hits[m['id']], 'reasons': reasons,
                          'source': (source or {}).get('import_path') or (source or {}).get('source', '')})
    finally:
        conn.close()
    data = json.dumps({'items': items, 'history': history, 'generated': datetime.now().isoformat(timespec='seconds')}, ensure_ascii=False).replace('<', '\\u003c').replace('>', '\\u003e').replace('&', '\\u0026')
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(TEMPLATE.replace('__DATA__', data), encoding='utf-8')
    return output


TEMPLATE = '''<!doctype html>
<html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>薪火 · 知识治理</title>
<style>
:root{--bg:#f2f0e9;--ink:#193831;--muted:#596c65;--line:#cbd3ca;--accent:#b44c2c}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font:15px/1.6 system-ui,sans-serif}main{max-width:1400px;margin:auto;padding:40px}header{border-top:5px solid var(--ink);padding:24px 0;display:flex;justify-content:space-between;gap:20px}h1{font:42px/1.2 Georgia,serif;margin:12px 0}h2{font-size:23px}p{color:var(--muted)}small{color:var(--muted)}.metrics{display:flex;border-block:1px solid var(--line);margin:16px 0 30px;flex-wrap:wrap}.metric{flex:1;min-width:150px;padding:20px;border-right:1px solid var(--line)}.metric b{display:block;font:36px Georgia,serif}.controls{display:flex;gap:16px;flex-wrap:wrap;margin-bottom:24px}input,select{font:inherit;padding:10px;border:1px solid var(--line);border-radius:4px;background:#fff;color:var(--ink)}input{min-width:260px}label{display:grid;gap:5px}table{border-collapse:collapse;width:100%;background:#fafbf7}th,td{text-align:left;padding:14px;border-bottom:1px solid var(--line);vertical-align:top}th{font-size:12px;color:var(--muted);white-space:nowrap}td small{display:block;max-width:420px;overflow-wrap:anywhere}.scroll{overflow:auto}summary{cursor:pointer;padding:15px 0}code{font-size:12px}.issue{color:var(--accent)}.empty{padding:32px;color:var(--muted)}@media(max-width:700px){main{padding:20px}header{display:block}h1{font-size:32px}.metric{min-width:45%}}
</style><main><header><div><small>KNOWBASE / 薪火</small><h1>让经验有据可循。</h1><p>知识质量与使用情况 · 本地治理快照</p></div><div><small id="time"></small><p>命中 → 读取 → 有效反馈<br>每一步分别计数。</p></div></header>
<div class="metrics" id="metrics"></div>
<div class="controls"><label>项目<select id="scope"><option value="">全部项目</option></select></label><label>知识视图<select id="view"><option value="all">全部知识</option><option value="used">高频有效反馈</option><option value="issues">疑似问题</option><option value="pending">待人工审核（staging）</option><option value="once">尚未验证（once）</option></select></label><label>搜索知识<input id="query" placeholder="标题、编号、标签或来源"></label></div>
<p id="count" aria-live="polite"></p><div class="scroll"><table><thead><tr><th>知识 / 来源</th><th>范围与状态</th><th>搜索命中</th><th>读取</th><th>有效反馈</th><th>关注事项</th></tr></thead><tbody id="rows"></tbody></table></div>
<h2>搜索命中记录</h2><p>展示最近 10,000 次搜索；本页命中计数也采用此范围。读取与有效反馈为累计值。旧日志未记录命中明细，无法补推历史结果。有效反馈是报告的使用效果，不代表独立验证或唯一任务数。</p><div id="history"></div><button id="more" type="button">加载更多记录</button>
</main><script type="application/json" id="data">__DATA__</script><script>
const data=JSON.parse(document.getElementById('data').textContent), $=id=>document.getElementById(id);
const text=(tag,value)=>{const el=document.createElement(tag);el.textContent=value;return el};
$('time').textContent='生成于 '+data.generated;
for(const scope of [...new Set(data.items.map(x=>x.scope))].sort()){const o=text('option',scope);o.value=scope;$('scope').append(o)}
let historyLimit=50;
function render(){const scope=$('scope').value,q=$('query').value.toLowerCase(),view=$('view').value;const base=data.items.filter(x=>!scope||x.scope===scope);
$('metrics').replaceChildren();for(const [name,n] of [['知识总量',base.length],['有有效反馈',base.filter(x=>x.helpful_count>0).length],['疑似问题',base.filter(x=>x.reasons.length).length],['待人工审核',base.filter(x=>x.staging).length]]){const e=text('div',name);e.className='metric';e.prepend(text('b',n));$('metrics').append(e)}
const items=base.filter(x=>(view==='all'||view==='used'&&x.helpful_count>0||view==='issues'&&x.reasons.length||view==='pending'&&x.staging||view==='once'&&x.confidence==='once')&&[x.title,x.id,x.source,...x.tags].join(' ').toLowerCase().includes(q)).sort((a,b)=>b.helpful_count-a.helpful_count||b.hit_count-a.hit_count);
$('count').textContent=items.length+' 条知识 · 按有效反馈、读取次数排序';$('rows').replaceChildren();
for(const x of items){const tr=document.createElement('tr'),title=text('td',x.title);title.append(text('small',x.id+' · '+x.source));tr.append(title);for(const v of [x.scope+' / '+(x.staging?'待审 / ':'')+x.confidence+' / '+x.status,x.search_hits,x.hit_count,x.helpful_count,x.reasons.join('；')||'—'])tr.append(text('td',v));$('rows').append(tr)}
if(!items.length){const tr=document.createElement('tr'),td=text('td','当前筛选没有知识');td.colSpan=6;td.className='empty';tr.append(td);$('rows').append(tr)}
const events=data.history.filter(e=>!scope||e.scope===scope);$('history').replaceChildren();for(const e of events.slice(0,historyLimit)){const d=document.createElement('details');d.append(text('summary',e.ts+' · '+e.agent+' · '+e.query+' · '+(e.hits===null?'历史明细缺失':(e.hits||[]).length+' 条命中')));d.append(text('p','入口：'+e.tool+' / scope：'+(e.scope||'未指定')));for(const h of e.hits||[])d.append(text('p','#'+h.rank+' '+h.id+' '+h.title+' · score '+h.score+' · '+h.confidence+'/'+h.status));$('history').append(d)}if(!events.length)$('history').append(text('p','暂无搜索记录'));$('more').hidden=events.length<=historyLimit;
}
for(const id of ['scope','view','query'])$(id).addEventListener('input',()=>{historyLimit=50;render()});$('more').onclick=()=>{historyLimit+=50;render()};render();
</script></html>'''
