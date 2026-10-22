"""离线 HTML 治理快照；无外部资源，不执行知识正文中的 HTML。"""
import json
from collections import Counter
from datetime import datetime
from pathlib import Path

from . import index, store


def generate(repo: Path, output: Path, *, team_snapshot: dict | None = None) -> Path:
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
        from .value import report as value_report
        personal = value_report.personal_summary(conn)
    finally:
        conn.close()
    if team_snapshot is not None:
        # 团队分享输出：命中计数保留在 items，原始查询明细不嵌入页面数据。
        history = []
    data = json.dumps({'items': items, 'history': history,
                       'generated': datetime.now().isoformat(timespec='seconds'),
                       'value': {'personal': personal, 'team': team_snapshot},
                       }, ensure_ascii=False).replace('<', '\\u003c').replace('>', '\\u003e').replace('&', '\\u0026')
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
<h2 id="value-head">价值与使用</h2><p id="value-note">新事件口径与上方治理累计值分属不同范围，不相加；时间/宿主/范围筛选作用于下方价值分区（治理区沿用自身筛选）。</p>
<div class="controls"><label>时间范围<select id="v-time"><option value="0">全部</option><option value="7">近 7 天</option><option value="30">近 30 天</option><option value="90">近 90 天</option></select></label><label>宿主<select id="v-host"><option value="">全部宿主</option></select></label><label>范围<select id="v-scope"><option value="all">本机＋团队</option><option value="local">仅本机</option><option value="team">仅团队</option></select></label></div>
<div id="localSections"><div id="valueTrend"></div><div id="valuePersonal"></div>
<h2>采集健康（本机）</h2><div id="valueHealth"></div></div>
<h2 id="team-head">团队快照</h2><div id="teamSection"></div>
</main><script type="application/json" id="data">__DATA__</script><script>
const data=JSON.parse(document.getElementById('data').textContent), $=id=>document.getElementById(id);
const text=(tag,value)=>{const el=document.createElement(tag);el.textContent=value;return el};
const table=(head,rows)=>{const t=document.createElement('table'),thead=text('thead',''),tr=text('tr','');for(const h of head)tr.append(text('th',h));thead.append(tr);const tb=text('tbody','');for(const r of rows){const b=text('tr','');for(const c of r)b.append(text('td',c));tb.append(b)}t.append(thead,tb);return t};
const pairs=p=>Object.entries(p||{}).filter(([,v])=>v).map(([k,v])=>k+' '+v).join('、')||'无';
const orUnknown=(v,alt)=>v==null?(alt||'unknown'):v;
$('time').textContent='生成于 '+data.generated;
for(const scope of [...new Set(data.items.map(x=>x.scope))].sort()){const o=text('option',scope);o.value=scope;$('scope').append(o)}
let historyLimit=50;
function render(){const scope=$('scope').value,q=$('query').value.toLowerCase(),view=$('view').value;const base=data.items.filter(x=>!scope||x.scope===scope);
$('metrics').replaceChildren();for(const [name,n] of [['知识总量',base.length],['有有效反馈',base.filter(x=>x.helpful_count>0).length],['疑似问题',base.filter(x=>x.reasons.length).length],['待人工审核',base.filter(x=>x.staging).length]]){const e=text('div',name);e.className='metric';e.prepend(text('b',n));$('metrics').append(e)}
const items=base.filter(x=>(view==='all'||view==='used'&&x.helpful_count>0||view==='issues'&&x.reasons.length||view==='pending'&&x.staging||view==='once'&&x.confidence==='once')&&[x.title,x.id,x.source,...x.tags].join(' ').toLowerCase().includes(q)).sort((a,b)=>b.helpful_count-a.helpful_count||b.hit_count-a.hit_count);
$('count').textContent=items.length+' 条知识 · 按有效反馈、读取次数排序';$('rows').replaceChildren();
for(const x of items){const tr=document.createElement('tr'),title=text('td',x.title);title.append(text('small',x.id+' · '+x.source));tr.append(title);for(const v of [x.scope+' / '+(x.staging?'待审 / ':'')+x.confidence+' / '+x.status,x.search_hits,x.hit_count,x.helpful_count,x.reasons.join('；')||'—'])tr.append(text('td',v));$('rows').append(tr)}
if(!items.length){const tr=document.createElement('tr'),td=text('td','当前筛选没有知识');td.colSpan=6;td.className='empty';tr.append(td);$('rows').append(tr)}
const events=data.history.filter(e=>!scope||e.scope===scope);$('history').replaceChildren();
if(data.value&&data.value.team){$('history').append(text('p','团队分享输出不嵌入本机搜索原文；本地生成（不带 --team-data）时保留搜索记录。'));}
else{for(const e of events.slice(0,historyLimit)){const d=document.createElement('details');d.append(text('summary',e.ts+' · '+e.agent+' · '+e.query+' · '+(e.hits===null?'历史明细缺失':(e.hits||[]).length+' 条命中')));d.append(text('p','入口：'+e.tool+' / scope：'+(e.scope||'未指定')));for(const h of e.hits||[])d.append(text('p','#'+h.rank+' '+h.id+' '+h.title+' · score '+h.score+' · '+h.confidence+'/'+h.status));$('history').append(d)}if(!events.length)$('history').append(text('p','暂无搜索记录'));}$('more').hidden=(data.value&&data.value.team)||events.length<=historyLimit;
}
for(const id of ['scope','view','query'])$(id).addEventListener('input',()=>{historyLimit=50;render()});$('more').onclick=()=>{historyLimit+=50;render()};render();
/* 价值分区：时间/宿主/范围联动筛选（作用于价值区；治理区沿用自身筛选） */
const vPersonal=data.value&&data.value.personal,vTeam=data.value&&data.value.team;
const trendSvg=(weekly,label)=>{if(!weekly||!weekly.length)return text('p',label+'：无周数据（不是零，是窗口内无事件）');const w=560,h=120,n=weekly.length,mx=Math.max(...weekly.map(x=>x.events),1);const px=i=>20+i*(w-40)/Math.max(n-1,1),py=v=>h-20-v*(h-40)/mx;const mk=f=>weekly.map((x,i)=>px(i)+','+py(x[f])).join(' ');const svg=document.createElementNS('http://www.w3.org/2000/svg','svg');svg.setAttribute('width',w);svg.setAttribute('height',h);svg.setAttribute('viewBox','0 0 '+w+' '+h);const line=(pts,stroke)=>{const pl=document.createElementNS('http://www.w3.org/2000/svg','polyline');pl.setAttribute('points',pts);pl.setAttribute('fill','none');pl.setAttribute('stroke',stroke);pl.setAttribute('stroke-width','2');svg.append(pl)};line(mk('events'),'#193831');line(mk('adopted'),'#b44c2c');const legend=text('p',label+'：— 事件（绿）/ — 采纳（红） · '+weekly.map(x=>x.week.split('-W')[1]+'W:'+x.events).join(' '));const wrap=document.createElement('div');wrap.append(svg,legend);return wrap};
const inTime=(at,days)=>{if(!days)return true;if(!at)return false;const t=Date.parse(at);return !isNaN(t)&&t>=Date.now()-days*864e5};
const weekOf=at=>{if(!at)return null;const d=new Date(at);if(isNaN(d))return null;const day=(d.getUTCDay()+6)%7;const mon=new Date(d);mon.setUTCDate(d.getUTCDate()-day);const thu=new Date(mon);thu.setUTCDate(mon.getUTCDate()+3);const y=thu.getUTCFullYear();const jan4=Date.UTC(y,0,4);const wk=1+Math.round((thu.getTime()-jan4)/6048e5);return y+'-W'+String(wk).padStart(2,'0')};
const weeklyOf=rows=>{const b={};for(const e of rows){const w=weekOf(e.occurred_at);if(!w)continue;const s=b[w]||(b[w]={week:w,events:0,adopted:0,feedback:0});s.events++;if(e.event_type==='knowledge.used'&&e.state==='adopted')s.adopted++;else if(e.event_type==='feedback.recorded')s.feedback++}return Object.values(b).sort((a,c)=>a.week<c.week?-1:1)};
function renderValue(){const days=parseInt($('v-time').value,10)||0,hostF=$('v-host').value,scopeF=$('v-scope').value;
$('localSections').hidden=scopeF==='team';$('team-head').hidden=scopeF==='local';$('teamSection').hidden=scopeF==='local';
/* 本机：趋势 + 汇总 + 事件明细（时间/宿主筛选联动：趋势与筛选态汇总随之重算） */
const host=$('valuePersonal');host.replaceChildren();$('valueTrend').replaceChildren();
if(!vPersonal){host.append(text('p','本机价值数据不可用（unknown）'))}
else{host.append(text('p','事件发射：'+(vPersonal.events_enabled?'已启用':'未启用（不是零事件；以下为既有记录）')+' · 窗口：'+(vPersonal.window.since||'全部')+' ~ '+(vPersonal.window.until||'最新')));
if(vPersonal.outbox){host.append(text('p','事件总数（全窗口口径）'+vPersonal.outbox.total+' · 数据水位 '+(vPersonal.outbox.watermark.min||'无')+' ~ '+(vPersonal.outbox.watermark.max||'无')));host.append(table(['事件类型','次数'],Object.entries(vPersonal.outbox.by_type).sort().map(([k,n])=>[k,n])))}else host.append(text('p','事件记录：unknown（表不存在，不是 0）'));
if(vPersonal.uses)host.append(text('p','消费回执（全窗口口径）：'+pairs(vPersonal.uses.by_state)+' · 任务 '+vPersonal.uses.tasks+'（其中采纳 '+vPersonal.uses.adopted_tasks+'）'));
if(vPersonal.feedback)host.append(text('p','反馈：'+pairs(vPersonal.feedback.by_outcome)+' · 绑定：'+pairs(vPersonal.feedback.by_binding)+'（仅版本/任务绑定驱动治理）'));
if(vPersonal.task_outcomes)host.append(text('p','任务结果：'+pairs(vPersonal.task_outcomes.by_result)+' · 验证来源：'+pairs(vPersonal.task_outcomes.by_source)));
if(vPersonal.commands){host.append(text('p','命令调用（尽力口径，不宣称完整）：共 '+vPersonal.commands.total+(vPersonal.commands.truncated?' · 仅列 Top '+vPersonal.commands.rows.length:'')));host.append(table(['入口','命令','状态','次数'],vPersonal.commands.rows.map(r=>[r.transport,r.command,r.status,r.count])))}
for(const n of vPersonal.notes||[])host.append(text('p','注意：'+n));
const re=vPersonal.recent_events;
if(re){const rows=re.events.filter(e=>inTime(e.occurred_at,days)&&(!hostF||e.host===hostF));
const filtered=days||hostF;
host.prepend(text('p',(filtered?'筛选态汇总（内嵌明细≤'+re.max+' 条重算'+(re.truncated?'，明细已截断，为部分视图':'')+'）：事件 '+rows.length+' · 采纳 '+rows.filter(e=>e.event_type==='knowledge.used'&&e.state==='adopted').length+' · 反馈 '+rows.filter(e=>e.event_type==='feedback.recorded').length:'当前未启用时间/宿主筛选；下方汇总为全窗口口径，明细与趋势随筛选联动')));
$('valueTrend').append(trendSvg(weeklyOf(rows),'本机周趋势（随筛选联动）'));
host.append(text('p','最近事件（筛选后 '+rows.length+' / 内嵌 '+re.events.length+' 条'+(re.truncated?'，明细截断至 '+re.max+'，更多走本地导出':'')+'）'));
host.append(table(['时间','类型','宿主','任务','状态'],rows.slice(-200).reverse().map(e=>[e.occurred_at||'未知',e.event_type,e.host||'未知',e.task_id||'—',e.state||'—'])))}}
const h=$('valueHealth');h.replaceChildren();
if(vPersonal){h.append(text('p','事件队列状态：'+pairs(vPersonal.outbox?vPersonal.outbox.by_state:{})));
h.append(text('p','成员归属：'+((vPersonal.identity.member_label||'').trim()?vPersonal.identity.member_label+'（'+(vPersonal.identity.member_evidence||'unknown')+'，'+(vPersonal.identity.member_confirmed_at||'时间未知')+'）':'未确认（unknown；工具/宿主标签不冒充成员）')));
h.append(text('p','安装实例：'+(vPersonal.identity.installation_id||'未生成（首次发射事件时生成）')))}
/* 团队：快照指标 + 周趋势 + 复用证据时间线 */
const s=$('teamSection');s.replaceChildren();
if(!vTeam){s.append(text('p','未提供团队快照：仅展示本机数据。生成：knowbase value snapshot --output <快照>，再 dashboard --team-data <快照>。'))}
else{s.append(text('p','范围：团队快照（团队 '+(vTeam.team_id||'')+'）· 窗口 '+(vTeam.window.since||'开始')+' ~ '+(vTeam.window.until||'最新')+' · 数据水位 '+(vTeam.data_watermark.max_event_time||'无')+' · 生成于 '+(vTeam.generated_at||'')+(days?'（时间筛选仅作用于下方周序列与证据时间线；汇总为快照冻结窗口口径，换窗口需重新生成快照）':'')));
if(vTeam.weekly&&vTeam.weekly.length){const wl=days?Math.ceil(days/7):0;s.append(trendSvg(wl?vTeam.weekly.slice(-wl):vTeam.weekly,'团队周趋势'))}
const m=vTeam.members||{};s.append(text('p','成员：观测 '+orUnknown(m.observed)+' · 有效消费 '+orUnknown(m.effective_consumers)+' · 覆盖率 '+(m.coverage_known&&m.coverage!=null?Math.round(m.coverage*100)+'%':'unknown（名单未登记或存在无归属安装，不估算）')));
const k=vTeam.tasks||{};s.append(text('p','任务：收到 '+orUnknown(k.delivered)+' · 采纳 '+orUnknown(k.adopted)+' · 采纳率 '+(k.adoption_rate!=null?Math.round(k.adoption_rate*100)+'%':'未知分母')));
const c=vTeam.cross_member_reuse||{};s.append(text('p','跨成员复用：'+(c.available?'任务 '+c.cross_member_tasks+'（未归属单列 '+c.unattributed_tasks+'）· 明确有帮助 '+c.helpful_tasks+' · Agent 单独声称 '+c.agent_reported_helpful_tasks+' · 冲突 '+c.conflicted_tasks+' · 任务失败剔除 '+c.failed_excluded_tasks+' · 反馈覆盖率 '+(c.feedback_coverage!=null?Math.round(c.feedback_coverage*100)+'%':'未知'):'不可计算——'+(c.reason||''))));
const cst=vTeam.cost;if(cst&&cst.available){const num=v=>v==null?'未采集':v;const models=Object.entries(cst.by_model).map(([mm,x])=>mm+'：请求 '+x.requests+'，输入 '+num(x.input_tokens)+'，输出 '+num(x.output_tokens)+'，缓存读 '+num(x.cache_read_tokens)+'，缓存写 '+num(x.cache_creation_tokens)).join('；');s.append(text('p','Token 成本（'+cst.currency+'）：'+models+'；归属：'+cst.attribution+' 级'))}
if(vTeam.sources&&vTeam.sources.length)s.append(table(['安装实例','事件数','最早事件','最晚事件'],vTeam.sources.map(x=>[x.installation_id||'unknown',x.events,x.first_event||'无',x.last_event||'无'])));
if(vTeam.reuse_evidence&&vTeam.reuse_evidence.length){s.append(text('p','复用证据链（每环节独立可缺失，缺失不补'+(days?'；时间线已按所选范围过滤':'')+'）：'));for(const ev of vTeam.reuse_evidence){const tl=ev.timeline.filter(e=>inTime(e.at,days));const d=document.createElement('details');d.append(text('summary','任务 '+(ev.task_id||'')+'（'+(ev.owner||'unknown')+'） · 消费 '+(ev.consumer||'unknown')+' · 卡 '+ev.cards.map(x=>x.memory_id+'←'+(x.contributor||'unknown')).join('，')));d.append(text('p','时间线：'+(tl.map(e=>e.at+' '+e.type+'（'+e.actor+'）').join(' → ')||'所选范围内无事件记录')));s.append(d)}if(vTeam.reuse_evidence_truncated)s.append(text('p','证据链截断至 '+vTeam.reuse_evidence.length+' 个任务。'))}
for(const n of vTeam.limitations||[])s.append(text('p','限制：'+n));
if(vTeam.task_details&&vTeam.task_details.length)s.append(text('p','任务明细 '+(vTeam.task_details_truncated?'（截断至 '+vTeam.task_details.length+' 条，更多走本地导出）':'')))}}
if(vPersonal&&vPersonal.recent_events){for(const hh of [...new Set(vPersonal.recent_events.events.map(e=>e.host).filter(Boolean))].sort()){const o=text('option',hh);o.value=hh;$('v-host').append(o)}}
for(const id of ['v-time','v-host','v-scope'])$(id).addEventListener('input',renderValue);renderValue();

</script></html>'''
