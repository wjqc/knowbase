"""Dense embedding（P2，H 阶段重构）。

Encoder 抽象：所有实现必须遵循 `EncoderProtocol`，
便于后续在不改动 channels / hybrid_search 的前提下切换真模型。

当前实现：
- `HashEncoder`：稳定 hash 桶 + 词频 + L2 归一化（无依赖占位）
- `TfidfEncoder`：基于 sklearn TfidfVectorizer + TruncatedSVD（LSA）
  的本地 dense embedding，零公网依赖，能给出明显优于 hash 的语义表征。

后续替换：sentence-transformers / bge / OpenAI 时仅需新增一个 Encoder。
"""
from __future__ import annotations

import hashlib
import math
from collections import Counter
from typing import Iterable, Protocol, runtime_checkable

from .tokenize import tokenize, tokenize_set


# ============================================================
# 默认占位实现：稳定 hash 桶（无依赖）
# ============================================================

_DEFAULT_DIM = 1024


def _hash_token(token: str, dim: int) -> int:
    h = hashlib.md5(token.encode("utf-8")).digest()
    return int.from_bytes(h[:4], "big") % dim


def _encode_hash(text: str, dim: int = _DEFAULT_DIM) -> list[float]:
    """文本 → L2 归一化 hash 向量。空文本返回全零。"""
    tokens = tokenize(text)
    if not tokens:
        return [0.0] * dim
    vec = [0.0] * dim
    for tok, cnt in Counter(tokens).items():
        vec[_hash_token(tok, dim)] += float(cnt)
    norm = math.sqrt(sum(x * x for x in vec))
    if norm > 0:
        vec = [x / norm for x in vec]
    return vec


def cosine(a: list[float], b: list[float]) -> float:
    """cosine = a·b（双方已 L2 归一化时等价）。"""
    return sum(x * y for x, y in zip(a, b))


# ============================================================
# Encoder 协议 + 默认实现
# ============================================================


@runtime_checkable
class EncoderProtocol(Protocol):
    """Dense embedding encoder 协议。

    实现要点：
    - encode 必须确定性：同 text → 同 vector（跨调用稳定）
    - 返回向量应 L2 归一化，便于 cosine = dot
    - model_version 用于索引元数据 / 双索引切换（V2 计划 §6.4）
    - dim 用于 self-check 与外部校验
    """

    model_version: str
    dim: int

    def encode(self, text: str) -> list[float]: ...


class HashEncoder:
    """稳定 hash 占位 encoder（同词 → 同向量，无外部依赖）。"""

    model_version = "hash-v1"
    dim = _DEFAULT_DIM

    def encode(self, text: str) -> list[float]:
        return _encode_hash(text, self.dim)


# ============================================================
# 本地 LSA encoder（sklearn TfidfVectorizer + TruncatedSVD）
# ============================================================


class TfidfEncoder:
    """基于 sklearn TfidfVectorizer + TruncatedSVD 的本地 dense encoder。

    特点：
    - 训练时：fit 到语料 → 得到 vocabulary + 隐语义
    - 推断时：单文本 → tfidf → svd → L2 归一化
    - 零公网依赖，可作为不上真模型时的"次优"选择

    使用：
        enc = TfidfEncoder(dim=128)
        enc.fit(["doc1 text", "doc2 text", ...])
        v = enc.encode("query text")
    """

    model_version = "tfidf-lsa-v1"

    def __init__(self, dim: int = 128):
        self.dim = dim
        self._vectorizer = None  # sklearn TfidfVectorizer
        self._svd = None  # sklearn TruncatedSVD
        self._fitted: bool = False

    def fit(self, corpus: Iterable[str]) -> "TfidfEncoder":
        from sklearn.decomposition import TruncatedSVD
        from sklearn.feature_extraction.text import TfidfVectorizer

        texts = [" ".join(tokenize(t)) for t in corpus]
        self._vectorizer = TfidfVectorizer(
            analyzer="word",
            token_pattern=r"(?u)\S+",  # 已预分词，直接按空格切
            min_df=1,
        )
        tfidf = self._vectorizer.fit_transform(texts)
        n_components = min(self.dim, max(1, min(tfidf.shape) - 1))
        self._svd = TruncatedSVD(n_components=n_components, random_state=42)
        self._svd.fit(tfidf)
        # dim 可能因 SVD 上限被截断，对齐
        self.dim = n_components
        self._fitted = True
        return self

    def _project(self, text: str) -> "list[float] | None":
        if not self._fitted:
            return None
        tokens = tokenize(text)
        if not tokens:
            return None
        joined = " ".join(tokens)
        tfidf = self._vectorizer.transform([joined])  # type: ignore[union-attr]
        if tfidf.nnz == 0:
            # OOV：完全没见过的 token 集合，返回 None 让上层降级
            return None
        reduced = self._svd.transform(tfidf)  # type: ignore[union-attr]
        return reduced[0].tolist()

    def encode(self, text: str) -> list[float]:
        v = self._project(text)
        if v is None:
            return [0.0] * self.dim
        norm = math.sqrt(sum(x * x for x in v))
        if norm > 0:
            v = [x / norm for x in v]
        return v


# ============================================================
# 工厂
# ============================================================


def default_encoder() -> EncoderProtocol:
    """返回默认 encoder（HashEncoder 占位）。

    升级到真模型时仅改这一处（返回 TfidfEncoder / SentenceTransformerEncoder 即可）。
    """
    return HashEncoder()


# 向后兼容：函数式入口，内部走 HashEncoder（dim=1024）
def encode(text: str, dim: int = _DEFAULT_DIM) -> list[float]:
    """文本 → L2 归一化 hash 向量。空文本返回全零。

    保留该函数以兼容旧测试 / 旧调用方；新代码请优先用 HashEncoder 实例。
    """
    return _encode_hash(text, dim)


__all__ = [
    "EncoderProtocol",
    "HashEncoder",
    "TfidfEncoder",
    "default_encoder",
    "encode",  # 向后兼容：函数式入口，内部走 HashEncoder
    "cosine",
]
