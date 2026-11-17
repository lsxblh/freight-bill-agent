"""票09：LLM 草稿缓存键含提示词指纹。

测试缝：``LlmExtractor.extract``（草稿 LLM 的受控面）。只把 ``_build``
（懒构造 LangChain 链）换成假链，绝不真联网；缓存读写走真实文件。

钉住三件事：
1. 提示词不变 → 同输入第二次命中缓存（缓存行为与之前一致）；
2. 提示词改动 → 旧草稿缓存自动失效、重新计算（否则 e2e 会复用旧提示词
   的 stale 草稿，看不到提示词改进）；
3. 旧格式键（无指纹，票09 之前的公式 model|filename|clipped）写好的
   stale 缓存文件不会被读到。
"""

from __future__ import annotations

import hashlib
from pathlib import Path

from freight_bill.config import Settings
from freight_bill.extract import llm_extractor
from freight_bill.extract.llm_extractor import LlmDocument, LlmExtractor

TEXT = "ARRIVAL NOTICE\nRELEASE DATE: 10/19/2026\nOn Carriage Additional USD 245.00"


def _settings(tmp_path: Path) -> Settings:
    """离线构造：启用 LLM 但 _build 会被假链替换，不会联网。"""
    return Settings(
        llm_enabled=True,
        llm_api_key="test-key",
        llm_cache_dir=tmp_path / "llm",
        work_dir=tmp_path / "work",
        output_dir=tmp_path / "out",
    )


class FakeChain:
    """假 LangChain 链：记录调用次数，返回固定草稿（document_date=None）。"""

    def __init__(self) -> None:
        self.calls: list[dict] = []

    def invoke(self, payload: dict) -> LlmDocument:
        self.calls.append(payload)
        return LlmDocument(document_date=None, confidence=0.9)


def test_cache_hit_when_prompt_unchanged(tmp_path: Path, monkeypatch) -> None:
    """提示词不变：同输入第二次命中缓存，不重复调模型。"""
    extractor = LlmExtractor(_settings(tmp_path))
    chain = FakeChain()
    monkeypatch.setattr(extractor, "_build", lambda: chain)

    first = extractor.extract(TEXT, filename="a.pdf", kind="pdf")
    second = extractor.extract(TEXT, filename="a.pdf", kind="pdf")

    assert first is not None and second is not None
    assert len(chain.calls) == 1
    assert extractor.stats["cache_hits"] == 1


def test_prompt_change_invalidates_cache(tmp_path: Path, monkeypatch) -> None:
    """提示词改动：旧缓存自动失效，重新调模型并写新键缓存。"""
    extractor = LlmExtractor(_settings(tmp_path))
    chain = FakeChain()
    monkeypatch.setattr(extractor, "_build", lambda: chain)

    extractor.extract(TEXT, filename="a.pdf", kind="pdf")
    # 模拟提示词改动（如票09 加日期语义约束）：指纹必须随之变化
    monkeypatch.setattr(
        llm_extractor, "SYSTEM_PROMPT", llm_extractor.SYSTEM_PROMPT + "\n测试用额外约束"
    )
    extractor.extract(TEXT, filename="a.pdf", kind="pdf")

    assert len(chain.calls) == 2
    assert extractor.stats["cache_hits"] == 0
    # 两次结果落在不同缓存键文件上
    assert len(list((tmp_path / "llm").glob("*.json"))) == 2


def test_stale_old_format_cache_not_reused(tmp_path: Path, monkeypatch) -> None:
    """旧格式键（无提示词指纹）的 stale 草稿不会被复用。

    按票09 之前的键公式 model|filename|clipped 预写一份 stale 草稿
    （document_date=2026-10-19，即 PDF1 误取 RELEASE DATE 的那份），
    新键必须绕过它重新计算。
    """
    settings = _settings(tmp_path)
    old_key = hashlib.sha256(
        f"{settings.llm_model}|a.pdf|{TEXT}".encode()
    ).hexdigest()
    cache_dir = tmp_path / "llm"
    cache_dir.mkdir(parents=True, exist_ok=True)
    stale = LlmDocument(document_date="2026-10-19", confidence=0.9)
    (cache_dir / f"{old_key}.json").write_text(stale.model_dump_json(), encoding="utf-8")

    extractor = LlmExtractor(settings)
    chain = FakeChain()
    monkeypatch.setattr(extractor, "_build", lambda: chain)

    result = extractor.extract(TEXT, filename="a.pdf", kind="pdf")

    assert result is not None
    assert len(chain.calls) == 1  # 未读 stale，真正重算
    assert result.document_date is None  # 不是 stale 里的 2026-10-19