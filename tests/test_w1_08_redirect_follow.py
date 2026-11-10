"""W1-8：redirect_ids 检索跟随。

覆盖：MERGE 后旧卡（archived）内容词检索返回新卡（archived 路）；
SPLIT 后旧卡（redirect 处置，仍 active）检索返回新卡；
follow_redirects 链式跟随 + 环保护 + scope 过滤。
"""
import json

from knowbase import index, store, writer
from knowbase.knowledge_lifecycle import changes, publication, recall, validation
from knowbase.migrations import apply_all
from knowbase.mutations import _compute_revision
from test_v2_06_07_08_content_loop import seed_evidence

BODY = ("## 结论\nCRLF 读视图不一致需入口归一。\n## 解决的问题\n跨平台协作。\n"
        "## 适用条件\nWindows 编辑器。\n## 不适用条件\n纯 LF 仓库。\n"
        "## 可执行动作\nread_text 统一 LF。\n## 关键证据\nPR-7 复现。\n"
        "## 验证情况\n双机验证。\n## 未知与待确认\n无。\n")


def make_card(repo, title, body=BODY, **kw):
    kw.setdefault("provenance", "受控回归样本 PR-7")
    result = writer.create(repo, "pitfall", title, body, scope="orders", **kw)
    assert not result.get("error"), result
    return result["entity_id"]


def head_of(conn, mid):
    return conn.execute("SELECT content_revision FROM entity_heads WHERE entity_id=?",
                        (mid,)).fetchone()[0]




def _merge_three(memory_repo, conn):
    """三卡 MERGE：a 为 canonical，b/c 归档带重定向（沿用 S3 形态）。"""
    a = make_card(memory_repo, "Git 邮箱门禁提交被拒排查")
    b = make_card(memory_repo, "push 因邮箱域校验失败处理",
                  body=BODY.replace("CRLF 读视图不一致需入口归一。",
                                    "乙专属词：push 邮箱域校验。"))
    c = make_card(memory_repo, "企业 Git 白名单不通过的绕行",
                  body=BODY.replace("CRLF 读视图不一致需入口归一。",
                                    "丙专属词：白名单绕行。"))
    heads = {m: head_of(conn, m) for m in (a, b, c)}
    for m in (a, b, c):
        seed_evidence(conn, m, heads[m])
    meta_a, body_a, _ = store.load(memory_repo, a)
    merged_body = BODY + "乙来源。\n## 例外\n内部域放行（丙）。\n"
    proposal = changes.propose_change(
        conn, memory_repo, relation="MERGE", risk="R2", scope="orders",
        base_heads=[{"memory_id": m, "revision_algo": "content-v1",
                     "content_revision": heads[m]} for m in (a, b, c)],
        outputs=[
            {"memory_id": a, "disposition": "active",
             "meta": {**meta_a, "updated": "2026-11-05"}, "body": merged_body},
            {"memory_id": b, "disposition": "alias", "meta": store.load(memory_repo, b)[0],
             "body": store.load(memory_repo, b)[1]},
            {"memory_id": c, "disposition": "alias", "meta": store.load(memory_repo, c)[0],
             "body": store.load(memory_repo, c)[1]}],
        source_dispositions=[
            {"memory_id": a, "action": "revise", "redirect_ids": []},
            {"memory_id": b, "action": "redirect", "redirect_ids": [a]},
            {"memory_id": c, "action": "redirect", "redirect_ids": [a]}],
        claim_map=[{"old_claim_id": "C-whole", "new_claim_ids": ["C-whole"],
                    "mapping": "preserved", "justification_ref": "source:merge"}])
    assert not proposal.get("error"), proposal
    check = validation.validate_change(conn, memory_repo, proposal["change_id"])
    assert check["result"] == "pass", check
    published = publication.publish(memory_repo, conn, proposal["change_id"])
    assert published["local_published"] and published["readback"]["ok"], published
    return a, b, c


def test_search_follows_archived_merge_redirect(memory_repo):
    conn = index.connect(memory_repo)
    apply_all(conn)
    a, b, c = _merge_three(memory_repo, conn)
    # 旧卡已归档，但其专属词仍应经重定向找到新卡。
    assert store.load(memory_repo, b)[0]["status"] == "archived"
    assert store.load(memory_repo, c)[0]["status"] == "archived"
    hits = index.search(conn, "乙专属词", scope="orders")
    ids = [h["id"] for h in hits]
    assert a in ids, hits
    target = next(h for h in hits if h["id"] == a)
    assert "redirect" in target["channels"]
    assert target["redirected_from"] == b
    # 旧卡本身不复活。
    assert b not in ids and c not in ids
    # 另一旧卡的专属词同样跟随。
    hits2 = index.search(conn, "丙专属词", scope="orders")
    assert a in [h["id"] for h in hits2]
    conn.close()


def test_follow_redirects_chain_cycle_and_scope(memory_repo):
    conn = index.connect(memory_repo)
    apply_all(conn)
    a = make_card(memory_repo, "链头卡")
    b = make_card(memory_repo, "链中卡")
    c = make_card(memory_repo, "链尾卡")
    # 手工登记：a→b→c，c→a 成环；另 b 跨 scope 目标（应被 scope 过滤）。
    conn.execute(
        "INSERT OR REPLACE INTO published_heads VALUES(?,?,?,?,?,?,?)",
        (a, "r1", "content-v1", "PUB-x", "redirect", json.dumps([b]), "h1"))
    conn.execute(
        "INSERT OR REPLACE INTO published_heads VALUES(?,?,?,?,?,?,?)",
        (b, "r1", "content-v1", "PUB-x", "redirect", json.dumps([c]), "h1"))
    conn.execute(
        "INSERT OR REPLACE INTO published_heads VALUES(?,?,?,?,?,?,?)",
        (c, "r1", "content-v1", "PUB-x", "redirect", json.dumps([a]), "h1"))
    conn.commit()
    followed = index.follow_redirects(conn, [a], scope="orders")
    assert [(f["target_id"], f["depth"]) for f in followed] == [(b, 1), (c, 2)]
    # frontmatter 兜底：无 published_heads 行时读旧卡 redirect_ids。
    conn.execute("DELETE FROM published_heads WHERE memory_id=?", (b,))
    meta_b, body_b, path_b = store.load(memory_repo, b)
    meta_b["redirect_ids"] = [c]
    path_b.write_text(store.render(meta_b, body_b), encoding="utf-8")
    index.upsert(conn, *store.load(memory_repo, b), commit=False)
    conn.commit()
    assert index.redirect_targets(conn, b) == [c]
    conn.close()


def test_evolution_recall_follows_redirect(memory_repo):
    conn = index.connect(memory_repo)
    apply_all(conn)
    # SPLIT：旧卡 d 保持 active、处置 redirect；e/f 为新卡。
    d = make_card(memory_repo, "旧排查手册",
                  body=BODY.replace("CRLF 读视图不一致需入口归一。",
                                    "旧手册专属词：全量排查。"))
    e = make_card(memory_repo, "故障定位命令手册",
                  body=BODY.replace("CRLF 读视图不一致需入口归一。",
                                    "旧手册专属词：全量排查（命令篇）。"))
    f = make_card(memory_repo, "故障恢复配置手册",
                  body=BODY.replace("CRLF 读视图不一致需入口归一。",
                                    "旧手册专属词：全量排查（配置篇）。"))
    heads = {m: head_of(conn, m) for m in (d, e, f)}
    seed_evidence(conn, d, heads[d])
    meta_d, body_d, _ = store.load(memory_repo, d)
    proposal = changes.propose_change(
        conn, memory_repo, relation="SPLIT", risk="R2", scope="orders",
        base_heads=[{"memory_id": d, "revision_algo": "content-v1",
                     "content_revision": heads[d]}],
        outputs=[
            {"memory_id": d, "disposition": "redirect",
             "meta": meta_d, "body": body_d},
            {"memory_id": e, "disposition": "active",
             "meta": store.load(memory_repo, e)[0], "body": store.load(memory_repo, e)[1]},
            {"memory_id": f, "disposition": "active",
             "meta": store.load(memory_repo, f)[0], "body": store.load(memory_repo, f)[1]}],
        source_dispositions=[{"memory_id": d, "action": "redirect",
                              "redirect_ids": [e, f]}],
        claim_map=[{"old_claim_id": "C-whole", "new_claim_ids": ["C-whole"],
                    "mapping": "preserved", "justification_ref": "source:split"}])
    assert not proposal.get("error"), proposal
    check = validation.validate_change(conn, memory_repo, proposal["change_id"])
    assert check["result"] == "pass", check
    published = publication.publish(memory_repo, conn, proposal["change_id"])
    assert published["local_published"], published
    assert store.load(memory_repo, d)[0]["status"] == "active"
    result = recall.evolution_recall(conn, "旧手册专属词 全量排查", "orders")
    ids = [c["id"] for c in result["candidates"]]
    assert e in ids and f in ids, result
    for cand in result["candidates"]:
        if cand["id"] in (e, f):
            assert "redirect" in cand["channels"]
            assert cand["redirected_from"] == d
    assert result["snapshot"]["redirect_added"] >= 2
    conn.close()
