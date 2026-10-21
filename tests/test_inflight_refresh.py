import json
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

from knowbase import config, gitops, index, refresh, store
from knowbase.locking import RepoLock
from knowbase.server import context_impl


def git(repo, *args):
    result = subprocess.run(["git","-C",str(repo),*args],capture_output=True,text=True,timeout=10)
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


def pair(repo, tmp_path):
    git(repo,"config","user.name","fixture")
    git(repo,"config","user.email","fixture@example.invalid")
    git(repo,"add","--","README.md",".gitignore")
    git(repo,"commit","--allow-empty","-m","initial fixture")
    git(repo,"branch","-M","main")
    bare=tmp_path/"remote.git"
    git(tmp_path,"init","--bare",str(bare))
    git(repo,"remote","add","origin",bare.as_uri())
    git(repo,"push","-u","origin","main")
    other=tmp_path/"other"
    git(tmp_path,"clone","--branch","main",str(bare),str(other))
    git(other,"config","user.name","peer-fixture")
    git(other,"config","user.email","peer@example.invalid")
    cfg=json.loads(config.CONFIG_PATH.read_text())
    cfg["git"].update(auto_pull=True,auto_push=False,remote={"name":"origin","branch":"main","url":bare.as_uri()},
                       allowed_remote_prefixes=["file://"],pull_ttl_seconds=60)
    config.CONFIG_PATH.write_text(json.dumps(cfg))
    return other


def publish(repo):
    meta=store.new_meta("pitfall","SSE 断线后应核对消息序号","orders",["SSE"],"agent:fixture:peer")
    meta["id"]="P-2026-9001"
    body="## 结论\nSSE 断线重连需按消息序号去重。\n## 适用条件\n测试订单消息流重连。\n"
    path=store.mem_path(repo,"pitfall",meta["id"])
    path.parent.mkdir(parents=True,exist_ok=True);path.write_text(store.render(meta,body))
    git(repo,"add","--",str(path.relative_to(repo)));git(repo,"commit","-m","peer knowledge")
    git(repo,"push","origin","main")
    return meta["id"]


def query(event):
    return json.loads(context_impl(session_id="refresh-session",event_id=event,scope="orders",
        current_goal="SSE 断线处理",topics=[{"topic_id":"sse","queries":["SSE 断线"],"status":"active"}]))


def test_context_async_refresh_consumes_peer_commit_and_invalidates_cache(memory_repo,tmp_path):
    peer=pair(memory_repo,tmp_path)
    mid=publish(peer)
    start=time.monotonic();first=query("first");elapsed=time.monotonic()-start
    assert elapsed < 1.0
    assert first["freshness"]["basis"] == "local_index"
    children=list(refresh._CHILDREN.values())
    assert children
    for child in children: assert child.wait(timeout=20) == 0
    second=query("after-worker")
    assert mid in [card["id"] for card in second["candidates"]]
    assert second["freshness"]["stale"] is False
    assert second["freshness"]["state"] == "fresh"
    assert git(memory_repo,"rev-parse","HEAD")==git(peer,"rev-parse","HEAD")


def test_slow_network_does_not_hold_repo_lock_or_block_query(memory_repo,tmp_path,monkeypatch):
    pair(memory_repo,tmp_path)
    scheduled=[]
    monkeypatch.setattr(refresh,"_launch",lambda *args:scheduled.append(args))
    started=time.monotonic();query("schedule-slow")
    assert time.monotonic()-started < 1.0
    entered,finish=threading.Event(),threading.Event()
    original=gitops._run
    def slow(repo,*args,**kwargs):
        if args[0]=="fetch":
            entered.set();assert finish.wait(5)
        return original(repo,*args,**kwargs)
    monkeypatch.setattr(gitops,"_run",slow)
    args=scheduled[0];result=[]
    worker=threading.Thread(target=lambda:result.append(refresh.run_once(*args)))
    worker.start()
    try:
        assert entered.wait(3)
        with RepoLock(memory_repo,timeout=0.1):
            pass
        start=time.monotonic();during=query("during-slow")
        assert time.monotonic()-start < 1.0
        assert during["freshness"]["state"] == "refresh_pending"
        assert len(scheduled)==1
    finally:
        finish.set();worker.join(10)
    assert result[0]["state"]=="completed"


def test_sync_lease_is_atomic_and_old_owner_cannot_release_takeover(memory_repo):
    with ThreadPoolExecutor(max_workers=8) as pool:
        claims=list(pool.map(lambda _:gitops.acquire_sync_lease(memory_repo,"origin","main"),range(12)))
    owners=[claim[0] for claim in claims if claim[0]]
    assert len(owners)==1
    old=owners[0]
    conn=index.connect(memory_repo)
    conn.execute("UPDATE sync_lease SET lease_until=?",((datetime.now(timezone.utc)-timedelta(seconds=1)).isoformat(),))
    conn.commit();conn.close()
    current,_=gitops.acquire_sync_lease(memory_repo,"origin","main")
    assert current and current!=old
    gitops._release_sync_lease(memory_repo,"origin","main",True,owner=old,cooldown=60)
    assert gitops._owns_sync_lease(memory_repo,"origin","main",current)
    assert refresh.run_once(memory_repo,"origin","main",old)["state"]=="lease_expired"


def test_refresh_failure_keeps_local_knowledge_and_respects_cooldown(memory_repo,tmp_path):
    pair(memory_repo,tmp_path)
    started=[]
    def fail(*args):
        started.append(args);raise OSError("controlled spawn failure")
    cfg=config.load_config()
    result=refresh.request(memory_repo,cfg,launcher=fail)
    assert result["state"]=="schedule_failed" and result["stale"]
    refresh.request(memory_repo,cfg,launcher=fail)
    assert len(started)==1
    assert git(memory_repo,"status","--porcelain")==""


def test_disabled_auto_pull_never_spawns(memory_repo):
    result=refresh.request(memory_repo,config.load_config(),launcher=lambda *args:(_ for _ in ()).throw(AssertionError()))
    assert result["state"]=="local_only" and result["stale"]


def test_read_refresh_does_not_push_local_commits_when_auto_push_disabled(memory_repo,tmp_path):
    pair(memory_repo,tmp_path)
    remote_head=git(memory_repo,"rev-parse","origin/main")
    git(memory_repo,"commit","--allow-empty","-m","local offline work")
    changed,warning=gitops.sync_before_read(memory_repo,config.load_config())
    assert not changed and not warning
    assert git(memory_repo,"rev-parse","origin/main")==remote_head
    assert git(memory_repo,"rev-parse","HEAD")!=remote_head


def test_failed_fetch_keeps_local_card_and_reports_last_success(memory_repo,tmp_path,monkeypatch):
    peer=pair(memory_repo,tmp_path);mid=publish(peer)
    assert gitops.sync_before_read(memory_repo,config.load_config())==(True,None)
    conn=index.connect(memory_repo)
    conn.execute("UPDATE sync_lease SET next_check_at=''");conn.commit();conn.close()
    original=gitops._run
    def offline(repo,*args,**kwargs):
        if args[0]=="fetch": raise RuntimeError("controlled offline")
        return original(repo,*args,**kwargs)
    monkeypatch.setattr(gitops,"_run",offline)
    def inline(*args): refresh.run_once(*args)
    monkeypatch.setattr(refresh,"_launch",inline)
    result=query("offline-lookup")
    assert mid in [card["id"] for card in result["candidates"]]
    assert result["freshness"]["last_success_at"]
    assert result["freshness"]["sync_status"]=="error"
    assert result["freshness"]["warning"]


def test_worker_cancelled_after_auto_pull_disabled(memory_repo,tmp_path):
    pair(memory_repo,tmp_path);scheduled=[]
    refresh.request(memory_repo,launcher=lambda *args:scheduled.append(args))
    cfg=config.load_config();cfg["git"]["auto_pull"]=False
    config.CONFIG_PATH.write_text(json.dumps(cfg))
    assert refresh.run_once(*scheduled[0])["state"]=="cancelled"
    assert not gitops._owns_sync_lease(*scheduled[0])


def test_remote_changed_after_scheduling_rejects_old_worker(memory_repo,tmp_path):
    pair(memory_repo,tmp_path);scheduled=[]
    refresh.request(memory_repo,launcher=lambda *args:scheduled.append(args))
    old_head=git(memory_repo,"rev-parse","HEAD")
    git(memory_repo,"remote","set-url","origin",(tmp_path/"different.git").as_uri())
    result=refresh.run_once(*scheduled[0])
    assert result["state"]=="failed"
    assert git(memory_repo,"rev-parse","HEAD")==old_head
    assert not gitops._owns_sync_lease(*scheduled[0])


def test_repo_lock_defers_query_without_marking_it_covered(memory_repo,tmp_path,monkeypatch):
    peer=pair(memory_repo,tmp_path);mid=publish(peer)
    assert gitops.sync_before_read(memory_repo,config.load_config())==(True,None)
    from knowbase import context
    from knowbase.locking import LockTimeout
    original=context._candidate
    monkeypatch.setattr(context,"_candidate",lambda *args:(_ for _ in ()).throw(LockTimeout("busy")))
    result=query("lock-busy")
    assert result["retrieval_status"]=="deferred_sync" and not result["candidates"]
    monkeypatch.setattr(context,"_candidate",original)
    resumed=query("lock-free")
    assert mid in [card["id"] for card in resumed["candidates"]]
