"""Reproducible synthetic benchmark, not a real-corpus relevance claim.
Run: python scripts/benchmark_document_library.py --files 500
"""
import argparse
import json
import statistics
import tempfile
import time
from pathlib import Path
from knowbase.document_library import DocumentLibrary


def run(count):
    with tempfile.TemporaryDirectory(prefix='knowbase-benchmark-') as temp:
        root = Path(temp) / 'inputs'
        root.mkdir()
        for i in range(count):
            (root / f'审批-{i:05}.md').write_text(
                f'# 审批流程 {i}\n\n' + '\n'.join(
                    f'第 {n} 步：审批凭证、异常回滚、重试幂等和权限审计记录。' for n in range(90)))
        lib = DocumentLibrary(Path(temp) / 'repo')
        def timed(fn):
            start = time.perf_counter()
            result = fn()
            return round(time.perf_counter()-start, 6), result
        initial, first = timed(lambda: lib.sync(root, scope='benchmark'))
        unchanged, second = timed(lambda: lib.sync(root, scope='benchmark'))
        (root / '审批-00000.md').write_text('# 单文件更新\n审批凭证更新完成\n')
        update, third = timed(lambda: lib.sync(root, scope='benchmark'))
        samples = [timed(lambda: lib.search_page('审批凭证', scope='benchmark', limit=20))[0] for _ in range(30)]
        page = lib.search_page('审批凭证', scope='benchmark', limit=20)
        return {'corpus':'synthetic Chinese Markdown, 90 lines/file', 'files':count,
                'initial_seconds':initial, 'unchanged_seconds':unchanged,
                'one_file_change_seconds':update, 'search_p50_seconds':statistics.median(samples),
                'search_p95_seconds':sorted(samples)[28], 'search_total':page['total'],
                'initial_report':first,'unchanged_report':second,'update_report':third}

if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--files', type=int, default=500)
    args = parser.parse_args()
    if not 1 <= args.files <= 10000:
        parser.error('--files must be 1..10000')
    print(json.dumps(run(args.files), ensure_ascii=False, indent=2))
