"""V2-03 检索评测质量门与影子运行：unknown 不凑样本、真实端到端延迟门、
任务簇 bootstrap、影子观测不产生递送。"""
import json

from knowbase import index, retrieval_metrics
from knowbase.retrieval_evaluation import (END_TO_END_P95_BUDGET_MS,
                                           measure_end_to_end_p95, paired_interval)
from test_local_retrieval_upgrade import seed


def _row(rid, relevant, cluster=None):
    return {'id': rid, 'cluster': cluster, 'labels': {'relevant_ids': relevant,
                                                      'unanswerable': not relevant}}


def test_paired_interval_uses_task_clusters():
    """同簇多条改写：按簇重抽样，method 声明簇数；结果确定。"""
    rows = [_row(f'r{i}', [f'P-{i}'], cluster='task-a') for i in range(3)] + \
           [_row(f'r{i}', [f'P-{i}'], cluster='task-b') for i in range(3, 6)]
    left = [[f'P-{i}'] for i in range(6)]
    right = [[] for _ in range(6)]
    first = paired_interval(rows, left, right)
    second = paired_interval(rows, left, right)
    assert first['clusters'] == 2
    assert 'task_cluster_bootstrap' in first['method']
    assert first == second  # 固定种子：同输入同结果
    # 无簇键：回退 query bootstrap 并声明仍需簇键。
    plain = [_row(f'r{i}', [f'P-{i}']) for i in range(6)]
    fell_back = paired_interval(plain, left, right)
    assert fell_back['clusters'] is None
    assert 'no_cluster_keys' in fell_back['method']


def test_paired_interval_same_cluster_rewrites_do_not_inflate_n():
    """6 条同簇改写的正例 n 仍逐 query 计（delta 层面），但簇数=1 时如实
    回退并声明——不伪装成 6 个独立任务的置信度。"""
    rows = [_row(f'r{i}', ['P-x'], cluster='only-task') for i in range(6)]
    result = paired_interval(rows, [['P-x']] * 6, [[]] * 6)
    assert result['clusters'] is None and 'no_cluster_keys' in result['method']


def test_measure_end_to_end_p95_without_model_is_lexical_only(memory_repo):
    seed(memory_repo)
    result = measure_end_to_end_p95(memory_repo, ['HAR 压缩排查', '死锁'])
    assert result['n'] == 2
    assert result['p95_ms'] is not None and result['p95_ms'] >= 0
    # 无模型/未激活 dense：basis 如实标 lexical_only，不能过 latency 门。
    assert result['basis'] == 'lexical_only'
    assert result['budget_ms'] == END_TO_END_P95_BUDGET_MS


def test_shadow_observations_never_deliver_or_govern(memory_repo):
    seed(memory_repo)
    conn = index.connect(memory_repo)
    before_usage = conn.execute("SELECT COUNT(*) FROM knowledge_uses").fetchone()[0]
    retrieval_metrics.shadow_observe(conn, 'hash-1',
                                     {'level': 'L2', 'reason': 'index_not_ready'})
    conn.commit()
    conn.close()
    report = retrieval_metrics.report(memory_repo)
    # 影子观测单列，不计入 executed_queries，不产生递送。
    assert report['shadow']['observations'] == 1
    assert report['shadow']['reasons'].get('index_not_ready') == 1
    assert report['shadow']['delivered'] == 0
    assert report['executed_queries'] == 0
    conn = index.connect(memory_repo)
    after_usage = conn.execute("SELECT COUNT(*) FROM knowledge_uses").fetchone()[0]
    deliveries = conn.execute("SELECT COUNT(*) FROM retrieval_deliveries").fetchone()[0]
    conn.close()
    assert after_usage == before_usage and deliveries == 0
