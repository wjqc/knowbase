"""Loopback-only, read-only browser for the local document library.

The browser is a source reader, not a knowledge-card editor. No model, remote
asset, document HTML rendering, or arbitrary-path endpoint is involved.
"""
from __future__ import annotations

import base64
import hashlib
import json
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

_STYLE = """
:root{font-family:system-ui,-apple-system,'Segoe UI',sans-serif;color:#26352e;background:#f3f5f0;font-size:15px}*{box-sizing:border-box}body{margin:0}button,input,select{font:inherit}button{cursor:pointer}header{padding:24px 32px 18px;border-bottom:1px solid #dce2d8;background:#fafbf7}h1{font-size:24px;margin:0 0 6px;letter-spacing:.03em}header p{margin:0;color:#67756a;font-size:13px}.toolbar{display:flex;gap:10px;margin-top:20px;max-width:1050px}.toolbar input{border:1px solid #cbd5c6;border-radius:8px;padding:11px 13px;background:white;min-width:0}.toolbar input[type=search]{flex:1}.toolbar input[name=scope]{width:170px}.primary{border:0;border-radius:8px;padding:10px 20px;background:#315e46;color:white}.secondary{border:1px solid #cbd5c6;background:white;color:#315e46;border-radius:8px;padding:8px 13px}main{display:grid;grid-template-columns:minmax(260px,340px) minmax(0,1fr);height:calc(100vh - 164px);min-height:400px}.sidebar{border-right:1px solid #dce2d8;overflow:auto;background:#fafbf7}.list-head{padding:16px 22px;font-size:13px;color:#647269;border-bottom:1px solid #e3e8de;display:flex;justify-content:space-between;align-items:center}.results{padding:10px}.result{display:block;text-align:left;width:100%;border:1px solid transparent;background:transparent;border-radius:8px;padding:14px;margin-bottom:5px;color:inherit}.result:hover{background:#eef2e9}.result[aria-current=true]{background:#e8efe2;border-color:#bacdb3}.result strong{display:block;font-weight:600;word-break:break-word}.meta{display:block;font-size:12px;color:#6f796e;margin-top:5px;word-break:break-all}.excerpt{font-size:12px;line-height:1.6;margin:9px 0 0;color:#68736a;white-space:pre-wrap;overflow-wrap:anywhere}.reader{min-width:0;min-height:0;display:flex;flex-direction:column;background:#fff}.reader-head,.reader>.notice{flex-shrink:0}.reader-head{padding:24px 30px 18px;border-bottom:1px solid #edf0e9}.reader-head h2{font-size:21px;margin:0 0 8px;word-break:break-word}.reader-actions{display:flex;flex-wrap:wrap;gap:10px;align-items:center;margin-top:14px}.source{overflow:auto;min-height:0;flex:1;padding:20px 20px 50px 10px;font:13px/1.85 ui-monospace,SFMono-Regular,Consolas,monospace}.line{display:grid;grid-template-columns:58px minmax(0,1fr);scroll-margin-top:20px}.line:target,.line.hit{background:#fff5c6}.line a{color:#9aa497;text-decoration:none;text-align:right;padding-right:17px;user-select:none}.line span{white-space:pre-wrap;overflow-wrap:anywhere}.empty{padding:70px 28px;max-width:620px;color:#748171;line-height:1.8}.empty strong{display:block;font-size:20px;color:#3b5544;margin-bottom:12px}.notice{margin:16px 24px;padding:12px 15px;border-radius:7px;background:#fff3d5;color:#775014;font-size:13px;line-height:1.7}.error{background:#fff0ec;color:#974731}.hidden{display:none!important}.sr-only{position:absolute;left:-9999px}.scope{color:#3b6547;font-size:11px;padding:2px 6px;background:#e8f0e3;border-radius:4px}.status{font-size:12px;color:#956e34}button:focus-visible,input:focus-visible,a:focus-visible{outline:3px solid #7a9a65;outline-offset:2px}@media(max-width:700px){header{padding:18px 16px}.toolbar{flex-wrap:wrap}.toolbar input[type=search]{flex-basis:65%}.toolbar input[name=scope]{width:115px}.toolbar button{padding:10px 13px}main{height:auto;display:block}.sidebar{max-height:330px;border-right:0;border-bottom:1px solid #dce2d8}.reader{min-height:400px}.reader-head{padding:20px}.source{padding-left:0}.line{grid-template-columns:43px minmax(0,1fr)}h1{font-size:21px}}
"""
_SCRIPT = r"""
const $ = (id) => document.getElementById(id);
let listRequest = 0,
    readRequest = 0,
    activeItem = null,
    currentDoc = null,
    listOffset = 0,
    listTotal = 0;
const statusLabels = { deleted: '源文件已移除', error: '索引失败' };
function elem(tag, text, cls) {
    const e = document.createElement(tag);
    if (text !== undefined) e.textContent = text;
    if (cls) e.className = cls;
    return e;
}
function notice(text, error = false) {
    $('notice').textContent = text;
    $('notice').className = 'notice' + (error ? ' error' : '');
}
async function api(path, params = {}) {
    const u = new URL(path, location.origin);
    for (const [k, v] of Object.entries(params))
        if (v !== undefined && v !== null && v !== '') u.searchParams.set(k, v);
    const r = await fetch(u, { credentials: 'omit', cache: 'no-store' });
    const data = await r.json();
    if (!r.ok) throw Error(data.error || '请求失败');
    return data;
}
function clearReader() {
    ++readRequest;
    activeItem = null;
    currentDoc = null;
    $('reader-content').replaceChildren();
    $('reader-head').classList.add('hidden');
    $('notice').classList.add('hidden');
    $('welcome').classList.remove('hidden');
}
async function loadList(offset = 0) {
    listOffset = offset;
    const ticket = ++listRequest;
    clearReader();
    const q = $('query').value.trim(),
        scope = $('scope').value.trim();
    $('list-label').textContent = '正在读取…';
    $('results').replaceChildren();
    try {
        const data = await api(
            q ? '/api/search' : '/api/documents',
            q ? { q, scope, offset: listOffset, limit: 30 } : { scope },
        );
        if (ticket !== listRequest) return;
        const items = data.items;
        listTotal = data.total || items.length;
        $('result-pages').classList.toggle('hidden', !q);
        $('results-prev').disabled = listOffset === 0;
        $('results-next').disabled = listOffset + items.length >= listTotal;
        $('results-range').textContent =
            (items.length ? listOffset + 1 : 0) +
            '–' +
            (listOffset + items.length) +
            ' / ' +
            listTotal;
        $('list-label').textContent =
            (q ? '检索结果' : '文档') +
            ' · ' +
            items.length +
            (items.length === (q ? 30 : 200) ? '（已达显示上限）' : '');
        if (!items.length) {
            $('results').append(
                elem(
                    'p',
                    q
                        ? '没有匹配内容。试试更短的词或切换范围。'
                        : '还没有文档。先在终端运行 knowbase documents sync 导入目录。',
                    'empty',
                ),
            );
            return;
        }
        for (const item of items) {
            const b = elem('button', undefined, 'result');
            b.type = 'button';
            b.append(
                elem(
                    'strong',
                    item.title || item.path || item.document_id || item.id,
                ),
            );
            b.append(elem('span', item.scope || '未设置范围', 'meta'));
            b.append(elem('span', item.path || '', 'meta'));
            if (item.start_line)
                b.append(
                    elem(
                        'span',
                        '原文行 ' + item.start_line + '–' + item.end_line,
                        'meta',
                    ),
                );
            if (
                item.status &&
                item.status !== 'available' &&
                item.status !== 'active'
            )
                b.append(
                    elem(
                        'span',
                        statusLabels[item.status] || item.status,
                        'status',
                    ),
                );
            if (item.snippet) b.append(elem('p', item.snippet, 'excerpt'));
            b.onclick = () => {
                for (const node of $('results').children)
                    node.removeAttribute('aria-current');
                b.setAttribute('aria-current', 'true');
                openDocument(item);
            };
            $('results').append(b);
        }
    } catch (e) {
        if (ticket !== listRequest) return;
        $('list-label').textContent = '读取失败';
        notice(e.message, true);
    }
}
async function openDocument(
    item,
    latest = false,
    pageStart = null,
    historical = false,
) {
    const ticket = ++readRequest;
    activeItem = item;
    currentDoc = null;
    $('citation').value = '';
    $('copy-citation').textContent = '复制引用';
    $('versions').replaceChildren();
    $('related-cards').replaceChildren();
    $('welcome').classList.add('hidden');
    $('reader-head').classList.add('hidden');
    $('reader-content').replaceChildren();
    notice('正在读取原文…');
    try {
        const start =
            pageStart ||
            (!latest && item.start_line
                ? Math.max(1, item.start_line - 20)
                : 1);
        const doc = await api('/api/document', {
            id: item.document_id || item.id,
            sha256: latest ? undefined : item.sha256,
            start_line: start,
            end_line: start + 199,
            historical: historical ? '1' : undefined,
        });
        if (ticket !== readRequest) return;
        currentDoc = doc;
        $('citation').value = doc.citation || '';
        $('related-cards').replaceChildren();
        for (const card of doc.related_cards || [])
            $('related-cards').append(
                elem(
                    'p',
                    typeof card === 'string'
                        ? card
                        : [card.title || card.id, card.status]
                              .filter(Boolean)
                              .join(' · '),
                    'meta',
                ),
            );
        loadHistory(doc, ticket);
        $('reader-head').classList.remove('hidden');
        $('previous-page').disabled = doc.start_line <= 1;
        $('next-page').disabled =
            !doc.total_lines || doc.end_line >= doc.total_lines;
        $('page-range').textContent =
            '第 ' +
            doc.start_line +
            '–' +
            doc.end_line +
            ' 行' +
            (doc.total_lines ? ' / 共 ' + doc.total_lines + ' 行' : '');
        $('doc-title').textContent = doc.title || doc.path || doc.id;
        $('doc-meta').textContent = [doc.scope, doc.format, doc.path]
            .filter(Boolean)
            .join(' · ');
        $('doc-hash').textContent = '快照 ' + (doc.sha256 || '').slice(0, 12);
        $('notice').classList.add('hidden');
        if (doc.historical)
            notice(
                '正在查看历史快照，内容不代表当前源文件。引用固定到此版本与解析文本行号。',
            );
        else if (doc.stale)
            notice(
                '源文件或索引版本已变化。下方是索引快照，请重新同步后再核对当前位置。',
            );
        else if (
            doc.status &&
            doc.status !== 'active' &&
            doc.status !== 'available'
        )
            notice('文档状态：' + doc.status + '。下方内容是保留的索引快照。');
        const lines = (doc.text || '').split('\n'),
            base = Number(doc.start_line) || 1;
        if (lines.length > 1 && lines[lines.length - 1] === '') lines.pop();
        for (let i = 0; i < lines.length; i++) {
            const n = base + i;
            const row = elem('div', undefined, 'line');
            row.id = 'line-' + n;
            if (
                !latest &&
                !doc.historical &&
                item.start_line &&
                n >= item.start_line &&
                n <= item.end_line
            )
                row.classList.add('hit');
            const anchor = elem('a', String(n));
            anchor.href = '#line-' + n;
            anchor.setAttribute('aria-label', '第 ' + n + ' 行');
            row.append(anchor, elem('span', lines[i] || ' '));
            $('reader-content').append(row);
        }
        $('reader-content').scrollTop = 0;
        if (!latest && !doc.historical && item.start_line) {
            const hit = $('line-' + item.start_line);
            if (hit)
                $('reader-content').scrollTop =
                    hit.offsetTop - $('reader-content').offsetTop - 60;
        }
    } catch (e) {
        if (ticket === readRequest) {
            currentDoc = null;
            $('reader-head').classList.remove('hidden');
            $('doc-title').textContent = item.title || '文档';
            $('doc-meta').textContent = '';
            $('doc-hash').textContent = '';
            $('page-range').textContent = '';
            $('previous-page').disabled = true;
            $('next-page').disabled = true;
            notice(
                e.message ===
                    'snapshot changed; refresh search before locating lines'
                    ? '索引快照已变化，旧的命中位置不能安全定位。请重新搜索，或读取当前索引。'
                    : e.message,
                true,
            );
        }
    }
}
$('search-form').onsubmit = (e) => {
    e.preventDefault();
    loadList();
};
$('refresh').onclick = () => loadList();
$('reload-doc').onclick = () => activeItem && openDocument(activeItem, true);
$('previous-page').onclick = () =>
    currentDoc &&
    openDocument(
        { ...activeItem, sha256: currentDoc.sha256 },
        false,
        Math.max(1, currentDoc.start_line - 200),
        currentDoc.historical,
    );
$('next-page').onclick = () =>
    currentDoc &&
    openDocument(
        { ...activeItem, sha256: currentDoc.sha256 },
        false,
        currentDoc.end_line + 1,
        currentDoc.historical,
    );

async function loadHistory(doc, ticket) {
    $('versions').replaceChildren();
    try {
        const data = await api('/api/history', { id: doc.id });
        if (ticket !== readRequest) return;
        for (const version of data.items) {
            const option = elem(
                'option',
                (version.current ? '当前 · ' : '历史 · ') +
                    version.sha256.slice(0, 12) +
                    (version.created_at ? ' · ' + version.created_at : ''),
            );
            option.value = version.sha256;
            option.dataset.current = String(version.current);
            option.selected = version.sha256 === doc.sha256;
            $('versions').append(option);
        }
    } catch (e) {
        if (ticket === readRequest)
            $('versions').append(elem('option', '历史记录不可用'));
    }
}
$('versions').onchange = () => {
    if (!currentDoc) return;
    const version = $('versions').selectedOptions[0];
    const { id, title } = currentDoc;
    openDocument(
        { id, title, sha256: version.value },
        false,
        1,
        version.dataset.current !== 'true',
    );
};
$('copy-citation').onclick = async () => {
    if (!currentDoc) return;
    try {
        await navigator.clipboard.writeText($('citation').value);
        $('copy-citation').textContent = '已复制';
    } catch (e) {
        $('citation').focus();
        $('citation').select();
        notice('请复制已选中的引用文本。');
    }
};
$('results-prev').onclick = () => loadList(Math.max(0, listOffset - 30));
$('results-next').onclick = () => loadList(listOffset + 30);
loadList();
"""
_HTML = """<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"><title>薪火 · 文档资料库</title><style>""" + _STYLE + """</style></head><body><header><h1>薪火 <span style="font-weight:400">文档资料库</span></h1><p>查找资料，核对出处。原始文档与已治理的知识卡片保持分离。</p><form id="search-form" class="toolbar"><label class="sr-only" for="query">搜索原文</label><input id="query" type="search" placeholder="搜索原文内容…" maxlength="500" autocomplete="off"><label class="sr-only" for="scope">项目范围</label><input id="scope" name="scope" placeholder="项目范围（可选）" maxlength="200"><button class="primary" type="submit">搜索</button></form></header><main><aside class="sidebar" aria-label="文档列表"><div class="list-head"><span id="list-label" aria-live="polite">文档</span><button id="refresh" class="secondary" type="button">刷新</button></div><div id="results" class="results"></div><div id="result-pages" class="list-head hidden"><button id="results-prev" class="secondary" type="button">上一页</button><span id="results-range" class="meta"></span><button id="results-next" class="secondary" type="button">下一页</button></div></aside><section id="reader" class="reader" aria-label="原文阅读器"><div id="welcome" class="empty">从左侧打开一份文档，或搜索关键词定位相关段落。这里展示解析后的文本快照；PDF、Office 等内容的行号指向解析文本，不等同于原文件页码。</div><div id="reader-head" class="reader-head hidden"><h2 id="doc-title"></h2><div id="doc-meta" class="meta"></div><div class="reader-actions"><label for="versions">快照版本</label><select id="versions" aria-label="选择快照版本"></select></div><div class="reader-actions"><input id="citation" readonly aria-label="固定快照引用" style="flex:1;min-width:0"><button id="copy-citation" class="secondary" type="button">复制引用</button></div><div id="related-cards" aria-label="关联知识卡片"></div><div class="reader-actions"><span id="doc-hash" class="meta"></span><button id="reload-doc" class="secondary" type="button">读取当前索引</button></div><div class="reader-actions"><button id="previous-page" class="secondary" type="button">上一段</button><span id="page-range" class="meta"></span><button id="next-page" class="secondary" type="button">下一段</button></div></div><div id="notice" class="notice hidden" role="status"></div><div id="reader-content" class="source"></div></section></main><script>""" + _SCRIPT + """</script></body></html>"""


def create_server(repo: Path, host: str = "127.0.0.1", port: int = 8765, *, library_factory=None):
    """Create a loopback HTTP server. ``port=0`` is useful for integration tests."""
    if host != "127.0.0.1":
        raise ValueError("文档浏览器仅允许绑定 127.0.0.1")
    if not isinstance(port, int) or not 0 <= port <= 65535:
        raise ValueError("port must be between 0 and 65535")
    if library_factory is None:
        from .document_library import DocumentLibrary
        library = DocumentLibrary(repo)

        def library_factory(_repo):
            return library
    root = Path(repo)
    script_hash = base64.b64encode(hashlib.sha256(_SCRIPT.encode()).digest()).decode()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            # Queries and source identifiers must not leak into console logs.
            pass

        def _reply(self, status, value, html=False):
            body = value.encode("utf-8") if html else json.dumps(value, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "text/html; charset=utf-8" if html else "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Content-Security-Policy", f"default-src 'none'; script-src 'sha256-{script_hash}'; style-src 'unsafe-inline'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
            self.end_headers()
            self.wfile.write(body)

        def _authorized(self):
            # Exact authority check prevents DNS rebinding; no wildcard localhost.
            expected = f"127.0.0.1:{self.server.server_port}"
            if self.headers.get_all("Host") != [expected]:
                self._reply(403, {"error": "仅允许本地浏览器访问"})
                return False
            origin = self.headers.get("Origin")
            if origin is not None and origin != "http://" + expected:
                self._reply(403, {"error": "不允许跨来源访问"})
                return False
            return True

        def do_GET(self):
            if not self._authorized():
                return
            try:
                url = urlsplit(self.path)
                if url.scheme or url.netloc or len(self.path) > 4096:
                    raise ValueError("无效请求")
                params = parse_qs(url.query, keep_blank_values=True, max_num_fields=12)
                if any(len(v) != 1 for v in params.values()):
                    raise ValueError("参数不可重复")
                values = {k: v[0] for k, v in params.items()}
                allowed = {"/": set(), "/api/documents": {"scope", "limit"},
                           "/api/search": {"q", "scope", "limit", "offset"},
                           "/api/history": {"id"},
                           "/api/document": {"id", "sha256", "start_line", "end_line", "historical"}}
                if url.path not in allowed:
                    self._reply(404, {"error": "未找到页面"}); return
                if set(values) - allowed[url.path]:
                    raise ValueError("未知参数")
                if url.path == "/":
                    self._reply(200, _HTML, html=True); return
                scope = values.get("scope", "")
                if len(scope) > 200:
                    raise ValueError("项目范围过长")
                def number(key, default, maximum):
                    raw = values.get(key)
                    if raw is None:
                        return default
                    if not re.fullmatch(r"[0-9]+", raw):
                        raise ValueError("无效数字参数")
                    result = int(raw)
                    if not 1 <= result <= maximum:
                        raise ValueError("数字参数超出范围")
                    return result
                library = library_factory(root)
                if url.path == "/api/documents":
                    payload = {"items": library.list_documents(scope=scope, limit=number("limit", 200, 200))}
                elif url.path == "/api/search":
                    query = values.get("q", "").strip()
                    if not query or len(query) > 500:
                        raise ValueError("搜索词需为 1–500 个字符")
                    if "offset" in values:
                        raw_offset = values["offset"]
                        if not re.fullmatch(r"[0-9]{1,7}", raw_offset):
                            raise ValueError("无效分页偏移")
                        payload = library.search_page(query, scope=scope, limit=number("limit", 30, 100), offset=int(raw_offset))
                    else:
                        payload = {"items": library.search(query, scope=scope, limit=number("limit", 30, 100))}
                else:
                    identifier = values.get("id", "")
                    if not identifier or len(identifier) > 200 or not re.fullmatch(r"[A-Za-z0-9_-]+", identifier):
                        raise ValueError("无效文档标识")
                    if url.path == "/api/history":
                        self._reply(200, {"items": library.history(identifier)})
                        return
                    historical = values.get("historical", "0")
                    if historical not in {"0", "1"}:
                        raise ValueError("无效历史快照参数")
                    sha = values.get("sha256")
                    if historical == "1" and sha is None:
                        raise ValueError("历史快照必须指定哈希")
                    if sha is not None and not re.fullmatch(r"[0-9a-f]{64}", sha):
                        raise ValueError("无效快照哈希")
                    start = number("start_line", None, 10_000_000)
                    end = number("end_line", None, 10_000_000)
                    if start is not None and end is not None and start > end:
                        raise ValueError("原文行范围无效")
                    kwargs = {"sha256": sha, "start_line": start, "end_line": end}
                    if historical == "1":
                        kwargs["historical"] = True
                    payload = library.read(identifier, **kwargs)
                    if payload is None:
                        raise KeyError(identifier)
                self._reply(200, payload)
            except (ValueError, TypeError) as exc:
                self._reply(400, {"error": str(exc)})
            except (KeyError, FileNotFoundError):
                self._reply(404, {"error": "文档或快照不存在，请刷新列表"})
            except Exception:
                self._reply(500, {"error": "无法读取文档索引，请检查索引状态后重试"})

        def do_POST(self):
            self._reply(405, {"error": "文档浏览器仅支持读取"})
        do_PUT = do_DELETE = do_PATCH = do_POST

    return ThreadingHTTPServer((host, port), Handler)


def serve(repo: Path, host: str = "127.0.0.1", port: int = 8765, open_browser: bool = False):
    """Serve until interrupted. Never modifies source files or knowledge cards."""
    import webbrowser
    server = create_server(repo, host, port)
    url = f"http://127.0.0.1:{server.server_port}/"
    print(f"文档资料库：{url}（仅本机，只读；Ctrl+C 停止）", flush=True)
    if open_browser:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
