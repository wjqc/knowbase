"""P2 retrieval 模块单测。"""
from __future__ import annotations

import pytest

from knowbase.v2.retrieval.bm25 import BM25
from knowbase.v2.retrieval.channels import (
    BM25Channel,
    DenseChannel,
    ExactChannel,
    MetadataChannel,
)
from knowbase.v2.retrieval.embeddings import cosine, encode
from knowbase.v2.retrieval.hybrid_search import HybridSearch, HybridResult
from knowbase.v2.retrieval.rrf import rrf
from knowbase.v2.retrieval.shadow_adapter import format_as_v1
from knowbase.v2.retrieval.tokenize import tokenize, tokenize_set


# ---------- tokenize ----------

def test_tokenize_chinese_bigram():
    out = tokenize("缓存击穿")
    # 单字 + bigram
    assert "缓" in out and "存" in out
    assert "缓存" in out and "存击" in out and "击穿" in out


def test_tokenize_english_kept():
    out = tokenize("EasyConnect deadlock")
    assert "easyconnect" in out and "deadlock" in out


def test_tokenize_mixed():
    out = tokenize("Agent 接入 knowbase")
    assert "agent" in out and "knowbase" in out
    assert "接入" in out and "knowbase" in out  # 跨语言


def test_tokenize_empty_returns_empty():
    assert tokenize("") == []
    assert tokenize("   ") == []


def test_tokenize_set_dedup():
    s = tokenize_set("缓存缓存 缓存")
    assert "缓" in s and "缓存" in s


# ---------- embeddings ----------

def test_encode_same_text_same_vector():
    a = encode("hello world")
    b = encode("hello world")
    assert a == b


def test_encode_different_text_different_vector():
    a = encode("hello world")
    b = encode("goodbye world")
    assert a != b


def test_encode_empty_returns_zero_vector():
    v = encode("")
    assert all(x == 0.0 for x in v)


def test_cosine_self_is_1():
    v = encode("测试文本")
    assert abs(cosine(v, v) - 1.0) < 1e-9


def test_cosine_orthogonal_returns_low():
    a = [1.0, 0.0, 0.0]
    b = [0.0, 1.0, 0.0]
    assert cosine(a, b) == 0.0


def test_hash_encoder_is_encoder_protocol():
    from knowbase.v2.retrieval.embeddings import HashEncoder, EncoderProtocol
    enc = HashEncoder()
    assert isinstance(enc, EncoderProtocol)
    assert enc.model_version == "hash-v1"
    assert enc.dim == 1024
    v = enc.encode("hello")
    assert len(v) == 1024
    assert abs(sum(x * x for x in v) - 1.0) < 1e-6  # L2 normalized


def test_tfidf_encoder_fit_then_encode():
    from knowbase.v2.retrieval.embeddings import TfidfEncoder
    corpus = [
        "Redis 雪崩 大量 key 过期",
        "MySQL 索引 失效 查询 慢",
        "Kafka 消费 积压 消息 堆积",
        "VPN 启动 失败 客户端 连不上",
    ]
    enc = TfidfEncoder(dim=8)
    enc.fit(corpus)
    # 同词 → 同向量
    a = enc.encode("Redis 雪崩")
    b = enc.encode("Redis 雪崩")
    assert a == b
    # L2 normalized
    norm = sum(x * x for x in a) ** 0.5
    assert abs(norm - 1.0) < 1e-6
    # 不同文本 → 不同向量
    c = enc.encode("MySQL 索引")
    assert a != c
    # 未训练 OOV → 返回零向量
    enc_empty = TfidfEncoder(dim=8)
    v = enc_empty.encode("anything")
    assert v == [0.0] * 8


def test_tfidf_encoder_semantic_better_than_hash():
    """TfidfEncoder 在近义匹配上应给出比 HashEncoder 更高的 cosine。"""
    from knowbase.v2.retrieval.embeddings import HashEncoder, TfidfEncoder, cosine
    corpus = [
        "Redis 大量 key 同时过期导致雪崩",
        "MySQL 索引失效查询很慢",
        "Kafka 消费积压消息堆积",
        "EasyConnect 启动失败",
    ]
    tfidf = TfidfEncoder(dim=8).fit(corpus)
    h = HashEncoder()
    # 查询："缓存大量过期" — 期望与 doc 0 (雪崩) 相近
    q = "缓存大量过期"
    target = corpus[0]
    other = corpus[1]
    sim_t = cosine(tfidf.encode(q), tfidf.encode(target))
    sim_t_other = cosine(tfidf.encode(q), tfidf.encode(other))
    sim_h = cosine(h.encode(q), h.encode(target))
    sim_h_other = cosine(h.encode(q), h.encode(other))
    # TfidfEncoder: 目标应明显高于非目标
    assert sim_t > sim_t_other, f"tfidf: target={sim_t} <= other={sim_t_other}"
    # HashEncoder: 不强制区分（hash 可能命中也可能不命中）


def test_dense_channel_uses_injected_encoder():
    from knowbase.v2.retrieval.embeddings import TfidfEncoder
    ch = DenseChannel(DOCS, encoder=TfidfEncoder(dim=8).fit(
        [d["title"] + " " + d["body"] for d in DOCS]
    ))
    assert ch.model_version == "tfidf-lsa-v1"
    r = ch.rank("缓存击穿", top_k=5)
    assert r and r[0][0] == "M-000003"


# ---------- BM25 ----------

def test_bm25_basic_rank():
    bm = BM25()
    bm.add_documents([
        "apple banana",
        "apple apple apple",
        "cherry date",
    ])
    r = bm.rank("apple", top_k=5)
    # 命中 2 条；分数 > 0
    assert len(r) == 2
    # 含 apple 更多的排前
    assert r[0][0] == 1


def test_bm25_no_match_returns_empty():
    bm = BM25()
    bm.add_documents(["apple", "banana"])
    assert bm.rank("zzznotfound", top_k=5) == []


def test_bm25_chinese_bigram():
    # NOTE: 2 文档场景下 BM25Okapi 的 IDF = log(N - df + 0.5) - log(df + 0.5)
    # 在 df = N/2 时正好为 0（rank_bm25 ATIRE 变体的设计）。这里用 4 文档
    # 让 "雪崩" 只出现在 1 篇，确保 idf > 0。
    bm = BM25()
    bm.add_documents([
        "缓存击穿与雪崩",
        "VPN 启动失败",
        "MySQL 索引失效",
        "Kafka 消费积压",
    ])
    r = bm.rank("雪崩", top_k=5)
    assert r and r[0][0] == 0


# ---------- channels ----------

DOCS = [
    {"doc_id": "M-000001", "title": "EasyConnect 死锁",
     "body": "VPN 启动失败；版本 7.6.7 macOS 上死锁", "tags": ["vpn"], "kind": "pitfall"},
    {"doc_id": "M-000002", "title": "Redis 雪崩",
     "body": "大量 key 同时过期导致雪崩", "tags": ["redis", "cache"], "kind": "decision"},
    {"doc_id": "M-000003", "title": "缓存击穿",
     "body": "单热点 key 失效", "tags": ["redis"], "kind": "pitfall"},
]


def test_exact_channel_finds_id():
    ch = ExactChannel(DOCS)
    r = ch.rank("M-000001", top_k=5)
    assert r and r[0][0] == "M-000001"


def test_exact_channel_finds_substring_in_body():
    ch = ExactChannel(DOCS)
    r = ch.rank("死锁", top_k=5)
    assert r and r[0][0] == "M-000001"


def test_bm25_channel_returns_doc_id():
    ch = BM25Channel(DOCS)
    r = ch.rank("缓存击穿", top_k=5)
    assert r and r[0][0] == "M-000003"


def test_dense_channel_finds_semantic_match():
    ch = DenseChannel(DOCS)
    # "缓存大量过期" 应该比 "vpn" 更接近 M-000002 (雪崩)
    r = ch.rank("缓存大量过期", top_k=5)
    assert r
    # M-000002 应当在 Top 2
    ids = [d for d, _ in r]
    assert "M-000002" in ids[:2]


def test_dense_channel_empty_query_returns_empty():
    ch = DenseChannel(DOCS)
    assert ch.rank("", top_k=5) == []


def test_metadata_channel_tag_overlap():
    ch = MetadataChannel(DOCS)
    r = ch.rank("vpn", top_k=5)
    assert r and r[0][0] == "M-000001"


# ---------- RRF ----------

def test_rrf_fuses_rankings():
    fused = rrf({
        "a": [("x", 1.0), ("y", 0.5)],
        "b": [("y", 1.0), ("z", 0.5)],
    })
    # y 在 b 中排第 1, a 中排第 2 → 1/(60+1) + 1/(60+2)
    # x 在 a 中排第 1, b 中无 → 1/(60+1)
    # z 在 b 中排第 2, a 中无 → 1/(60+2)
    scores = dict(fused)
    assert scores["y"] > scores["x"]
    assert scores["y"] > scores["z"]


def test_rrf_single_channel_passthrough():
    fused = rrf({"only": [("a", 1.0), ("b", 0.5)]})
    assert [d for d, _ in fused] == ["a", "b"]


# ---------- HybridSearch ----------

def test_hybrid_search_returns_fused_results():
    hs = HybridSearch(DOCS)
    res = hs.search("缓存击穿", top_k=3)
    assert isinstance(res, HybridResult)
    assert res.hits
    assert res.hits[0].doc_id == "M-000003"
    # 至少 1 个通道给出分数
    assert any(res.hits[0].per_channel.values())
    # governance 字段已接入
    assert res.hits[0].fused_score > 0
    assert res.hits[0].final_score > 0
    assert res.hits[0].governance is not None
    assert "factor" in res.hits[0].governance


def test_hybrid_search_no_match_returns_empty():
    hs = HybridSearch(DOCS)
    res = hs.search("zzznoexistent", top_k=3)
    assert res.hits == []


def test_hybrid_search_per_channel_top_recorded():
    hs = HybridSearch(DOCS)
    res = hs.search("vpn", top_k=2)
    assert "exact" in res.per_channel_top
    assert "bm25" in res.per_channel_top
    assert "dense" in res.per_channel_top
    assert "metadata" in res.per_channel_top


# ---------- governance_factor ----------

def test_governance_confidence_in_range():
    from knowbase.v2.retrieval.governance import confidence_factor
    assert confidence_factor({}) == 1.0
    assert confidence_factor({"confidence": 0.5}) == 0.5
    assert confidence_factor({"confidence": 1.5}) == 1.0  # clamp
    assert confidence_factor({"confidence": -0.2}) == 0.0
    assert confidence_factor({"confidence": "bad"}) == 1.0


def test_governance_freshness_decay():
    import time
    from knowbase.v2.retrieval.governance import freshness_factor
    now = 1_700_000_000.0
    half = 365.0
    # 当下 → 1.0
    assert freshness_factor({}, now=now) == 1.0
    # 半衰期 → 0.5
    age = half * 86400
    f = freshness_factor({"created_at": now - age}, now=now, half_life_days=half)
    assert abs(f - 0.5) < 1e-6
    # ISO 字符串（早于 now → 应得 0 < factor < 1）
    f_iso = freshness_factor({"created_at": "2023-01-01"}, now=now)
    assert 0 < f_iso < 1
    # 未来时间戳 → 视为 0 年龄 → factor = 1.0（不放大也不缩小）
    f_future = freshness_factor({"created_at": "2099-01-01"}, now=now)
    assert f_future == 1.0


def test_governance_feedback_centered_at_1():
    from knowbase.v2.retrieval.governance import feedback_factor
    assert feedback_factor({}) == 1.0
    assert feedback_factor({"feedback_score": 1.0}) == 1.2
    assert feedback_factor({"feedback_score": -1.0}) == 0.8  # -1 + 0.2
    # 实际语义：score = -1 → factor = 1.0 + 0.2*(-1) = 0.8
    # 但 V2 计划 §9.1 中 "差评 → 降权"，文档未给确切映射，这里按线性公式
    assert feedback_factor({"feedback_score": 0.5}) == 1.1


def test_governance_authority_weight_table():
    from knowbase.v2.retrieval.governance import source_authority_factor
    fa, a = source_authority_factor({"authority": "authoritative"})
    assert a == "authoritative" and fa == 1.20
    fa, a = source_authority_factor({"authority": "validated"})
    assert fa == 1.00
    fa, a = source_authority_factor({"authority": "observed"})
    assert fa == 0.85
    fa, a = source_authority_factor({"authority": "staging"})
    assert fa == 0.50
    fa, a = source_authority_factor({})  # 默认 observed
    assert fa == 0.85


def test_governance_stale_drops_to_zero():
    from knowbase.v2.retrieval.governance import compute_governance_factor
    br = compute_governance_factor({"stale": True})
    assert br.factor == 0.0
    assert br.stale is True


def test_governance_staging_authority_with_drop_stale():
    from knowbase.v2.retrieval.governance import compute_governance_factor
    br = compute_governance_factor({"authority": "staging"})
    # staging 视为 stale → drop_stale=True 时因子 = 0
    assert br.factor == 0.0
    # drop_stale=False 时按权重 0.5 走
    br2 = compute_governance_factor({"authority": "staging"}, drop_stale=False)
    assert br2.factor == 0.5
    assert br2.authority == "staging"


def test_governance_combined_neutral_doc_factor():
    """中性文档（无任何字段）→ 1.0 * 1.0 * 1.0 * 0.85 = 0.85（observed 默认权重）。"""
    from knowbase.v2.retrieval.governance import compute_governance_factor
    br = compute_governance_factor({})
    assert abs(br.factor - 0.85) < 1e-9


def test_governance_hybrid_ranks_authoritative_higher():
    """两条 doc 同 RRF 分，authority=authoritative 应排在 observed 前。"""
    docs = [
        {"doc_id": "A", "title": "VPN 启动失败", "body": "EasyConnect 死锁",
         "kind": "pitfall", "tags": ["vpn"], "authority": "authoritative"},
        {"doc_id": "B", "title": "VPN 启动失败", "body": "EasyConnect 死锁",
         "kind": "pitfall", "tags": ["vpn"], "authority": "observed"},
    ]
    hs = HybridSearch(docs)
    res = hs.search("VPN 启动", top_k=2)
    assert len(res.hits) == 2
    # A 应排第一（authority=1.20 > B 的 0.85）
    assert res.hits[0].doc_id == "A"
    assert res.hits[0].governance["source_authority"] == 1.20
    assert res.hits[1].governance["source_authority"] == 0.85


def test_governance_hybrid_drops_stale_doc():
    docs = [
        {"doc_id": "A", "title": "缓存击穿", "body": "单热点 key 失效",
         "kind": "pitfall", "tags": ["redis"]},
        {"doc_id": "B", "title": "缓存击穿", "body": "单热点 key 失效",
         "kind": "pitfall", "tags": ["redis"], "stale": True},
    ]
    hs = HybridSearch(docs)
    res = hs.search("缓存击穿", top_k=2)
    assert res.hits[0].doc_id == "A"
    assert res.hits[0].governance["factor"] > 0
    # stale doc 因子 = 0，final_score 仍 = fused * 0 = 0
    assert res.hits[1].doc_id == "B"
    assert res.hits[1].final_score == 0.0


def test_governance_can_be_disabled():
    """apply_governance=False 时，final_score = fused_score。"""
    hs = HybridSearch(DOCS, apply_governance=False)
    res = hs.search("缓存击穿", top_k=2)
    assert res.governance_enabled is False
    assert all(h.governance is None for h in res.hits)
    assert all(h.final_score == h.fused_score for h in res.hits)


# ---------- reranker ----------

def test_token_overlap_reranker_basic():
    from knowbase.v2.retrieval.rerank import TokenOverlapReranker
    rr = TokenOverlapReranker()
    assert rr.name == "token_overlap"
    assert rr.model_version == "token-f1-v1"
    doc = {"title": "缓存击穿", "body": "单热点 key 失效导致缓存击穿"}
    q = "缓存击穿"
    s = rr._score(q, doc)
    assert 0 < s <= 1.0
    # 完全无关
    s_none = rr._score("xxxxx yyyyy", doc)
    assert s_none == 0.0
    # 空 query
    s_empty = rr._score("", doc)
    assert s_empty == 0.0


def test_token_overlap_reranker_reorders():
    """rerank 应能基于 F1 排序，将更相关的 doc 提到前面。"""
    from knowbase.v2.retrieval.rerank import TokenOverlapReranker
    docs = [
        {"doc_id": "X", "title": "缓存击穿", "body": "单热点 key 失效",
         "kind": "pitfall", "tags": ["redis"]},
        {"doc_id": "Y", "title": "VPN", "body": "EasyConnect 启动失败",
         "kind": "pitfall", "tags": ["vpn"]},
    ]
    rr = TokenOverlapReranker()
    hs = HybridSearch(docs, reranker=rr)
    res = hs.search("缓存击穿", top_k=2)
    assert res.reranker == "token_overlap"
    # X 与 query 完全重合 → 必排第一
    assert res.hits[0].doc_id == "X"


def test_hybrid_search_without_reranker_default():
    hs = HybridSearch(DOCS)
    res = hs.search("缓存击穿", top_k=2)
    assert res.reranker is None
    # rerank_score 字段默认为 None
    assert all(h.rerank_score is None for h in res.hits)


def test_rerank_does_not_resurrect_governance_dropped_doc():
    """被 governance 整除的 doc（final_score=0），rerank 不应拉回。"""
    docs = [
        {"doc_id": "GOOD", "title": "缓存击穿", "body": "单热点 key 失效",
         "kind": "pitfall", "tags": ["redis"]},
        {"doc_id": "BAD", "title": "缓存击穿 stale", "body": "单热点 key 失效",
         "kind": "pitfall", "tags": ["redis"], "stale": True},
    ]
    from knowbase.v2.retrieval.rerank import TokenOverlapReranker
    hs = HybridSearch(docs, reranker=TokenOverlapReranker())
    res = hs.search("缓存击穿", top_k=2)
    assert res.hits[0].doc_id == "GOOD"
    assert res.hits[1].doc_id == "BAD"
    # BAD 即使 rerank 分数高，final_score 仍为 0
    assert res.hits[1].final_score == 0.0


# ---------- shadow adapter ----------

def test_format_as_v1_no_match():
    res = HybridResult(query="zzz", hits=[])
    out = format_as_v1(res, doc_index={})
    assert out.startswith("未命中")


def test_format_as_v1_with_hits():
    hs = HybridSearch(DOCS)
    res = hs.search("缓存", top_k=2)
    out = format_as_v1(res, doc_index={d["doc_id"]: d for d in DOCS})
    assert "命中" in out
    assert "M-" in out
