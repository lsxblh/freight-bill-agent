"""票02：四段式 LangGraph 骨架验收测试。

测试缝（与规格 Testing Decisions 一致）：
* 主缝 ``BillingAgent(settings).run(paths)``：行为回归只从外部结果断言；
* 图缝 ``BillingAgent.graph()``：票02 的交付物是"内部编排为四段式
  StateGraph、条件边预留"，这是唯一允许观察内部结构的地方。

前两条是行为钉子：搬骨架最容易破坏"逐条费用 装配→落账→登记重复"的
交错顺序（匹配读的是活台账，先落账的 CREATE 行可能被同单据后一条
费用认亲）。它们在主缝上注入假客户端，只断言外部行为。
"""

from __future__ import annotations

from pathlib import Path
from unittest import mock

from langgraph.graph.state import CompiledStateGraph

from shipping_billing.config import Settings
from shipping_billing.extract.llm_extractor import LlmCharge, LlmDocument
from shipping_billing.ingest.ocr_client import OcrOutcome
from shipping_billing.pipeline import BillingAgent

#: 假 OCR 文本：规则抽不出任何字段，单据内容完全由假 LLM 草稿提供。
CANNED_OCR_TEXT = (
    "Canned OCR text for the graph skeleton test.\n"
    "No bill fields can be parsed from this text.\n"
)


class FakeOcrClient:
    """假 OCR：受控面与 OcrClient 一致（settings/process_file/_load_cache/stats）。"""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.stats = {"api_calls": 0, "cache_hits": 0, "failures": 0, "replays": 0}

    def process_file(self, path: Path) -> OcrOutcome:
        return OcrOutcome(text=CANNED_OCR_TEXT, confidence=0.99, source="api")

    def _load_cache(self, sha: str) -> None:
        return None


class FakeLlmExtractor:
    """假 LLM：受控面与 LlmExtractor 一致（enabled/extract/stats）。"""

    enabled = True

    def __init__(self, draft: LlmDocument) -> None:
        self._draft = draft
        self.stats = {"calls": 0, "cache_hits": 0, "failures": 0, "repairs": 0}

    def extract(self, text: str, *, filename: str, kind: str) -> LlmDocument:
        self.stats["calls"] += 1
        return self._draft


def _draft(charges: list[LlmCharge], total: float) -> LlmDocument:
    """构造规则草稿为空时的完整 LLM 草稿。"""
    return LlmDocument(
        reference_no="FAKE-GRAPH-001",
        document_date="2026-11-01",
        document_type="Debit Note",
        issuer="NEPTUNE SHIPPING LIMITED",
        recipient="ALL AMERICA FORWARDING, INC.",
        currency="USD",
        total_amount=total,
        charges=charges,
        confidence=0.9,
    )


def _run_with_draft(settings: Settings, samples_dir: Path, draft: LlmDocument):
    agent = BillingAgent(
        settings, ocr_client=FakeOcrClient(settings), llm=FakeLlmExtractor(draft)
    )
    result = agent.run([samples_dir / "图片示例.png"])
    assert not result.failures
    return agent, result


def test_intra_doc_duplicate_charge_goes_pending_review(
    settings: Settings, samples_dir: Path
) -> None:
    """同一单据内两条完全相同的费用：第二条必须被识别为重复转人工。

    这钉住"装配一条→落账→登记重复→再装配下一条"的交错顺序：
    若把登记重复推迟到整单装配完之后，第二条就会漏判。
    """
    charge = LlmCharge(
        raw_name="Handling Fee", amount=88.88, currency="USD",
        evidence="HANDLING FEE USD 88.88",
    )
    draft = _draft([charge, charge], total=177.76)
    _, result = _run_with_draft(settings, samples_dir, draft)

    assert [r.operation for r in result.records] == ["CREATE", "PENDING_REVIEW"]
    assert any(
        "重复" in reason for reason in result.records[1].audit_trail.ai_reasoning
    )


def test_same_name_charges_interleave_with_ledger(
    settings: Settings, samples_dir: Path
) -> None:
    """同单据同名不同额的两条费用：第一条 CREATE 落账后，第二条应能认亲它。

    匹配读的是活台账：若把落账推迟到整单装配完之后，第二条将匹配不到
    第一条刚新增的行，两条都会变成 CREATE——行为就变了。
    """
    draft = _draft(
        [
            LlmCharge(raw_name="Handling Fee", amount=88.88, currency="USD",
                      evidence="HANDLING FEE USD 88.88"),
            LlmCharge(raw_name="Handling Fee", amount=99.99, currency="USD",
                      evidence="HANDLING FEE USD 99.99"),
        ],
        total=188.87,
    )
    _, result = _run_with_draft(settings, samples_dir, draft)

    assert [r.operation for r in result.records] == ["CREATE", "UPDATE"]
    # 第二条按规则1（单据编号+费用名称）认亲到第一条刚新增的台账行
    assert result.records[1].audit_trail.match_rule == "单据编号+费用名称"
    assert [c["field"] for c in result.records[1].audit_trail.field_changes] == ["amount"]


def test_orchestration_is_four_stage_stategraph(settings: Settings) -> None:
    """内部编排必须是四段式 StateGraph，且条件边预留。"""
    agent = BillingAgent(settings)
    graph = agent.graph()
    assert isinstance(graph, CompiledStateGraph)

    drawn = graph.get_graph()
    assert {"ingest", "extract", "synthesize", "tail"} <= set(drawn.nodes)

    conditional = {(e.source, e.target) for e in drawn.edges if e.conditional}
    # 失败隔离条件边：读进来 / 双路抽取失败时直达结束，不拿垃圾跑下游
    assert ("ingest", "__end__") in conditional
    assert ("extract", "__end__") in conditional
    # 综合⇄尾巴的逐条循环边（票03/04 在此基础上接入软失败闸门与综合 agent）
    assert ("tail", "synthesize") in conditional


def test_run_goes_through_stategraph(settings: Settings, samples_dir: Path) -> None:
    """run() 必须真正经过 StateGraph，而不是旁路旧循环。"""
    agent = BillingAgent(settings)
    graph = agent.graph()
    with mock.patch.object(graph, "invoke", wraps=graph.invoke) as spy:
        result = agent.run([samples_dir / "图片示例.png"])
    assert spy.call_count == 1
    assert len(result.records) == 2