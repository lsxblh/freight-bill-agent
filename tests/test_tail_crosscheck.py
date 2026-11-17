"""票05：audit_trail 补齐 + 死规则尾巴钱关键项交叉验证。

测试缝（与规格 Testing Decisions 一致，不新增内部缝）：
* 主缝 ``BillingAgent(settings, *, ocr_client, llm, agent_llm).run(paths)``；
* OCR/草稿 LLM/综合 agent LLM 全部注入假实现，绝不真联网；
* 只断言外部行为：records / operation / audit_trail / ledger。

交叉验证对确定性链与综合 agent 两路一致生效（都经 assemble_record→tail）。
"""

from __future__ import annotations

import json
from pathlib import Path

from freight_bill.config import Settings
from freight_bill.extract.llm_extractor import LlmCharge, LlmDocument
from freight_bill.ingest.ocr_client import OcrOutcome
from freight_bill.pipeline import BillingAgent

#: 假 OCR 默认文本：规则抽不出任何字段，单据内容完全由假 LLM 草稿提供。
CANNED_OCR_TEXT = (
    "Canned OCR text for the tail crosscheck test.\n"
    "No bill fields can be parsed from this text.\n"
)

#: 规则可解析的单据文本：含 SUBTOTAL/TAX（无 TOTAL 模式），用于验证
#: audit_trail 的小计/税额来自单据草稿；费用行带数量/单价与原文片段。
RULES_TEXT_WITH_TOTALS = (
    "DEBIT NOTE\n"
    "D/C No.: DC-SUB-001\n"
    "D/C DATE: 2026-11-01\n"
    "BALANCE DUE TO NEPTUNE SHIPPING LIMITED\n"
    "BILL TO: ALL AMERICA FORWARDING, INC.\n"
    "HANDLING CHARGE - 100.00 1.00 C 100.00\n"
    "SUBTOTAL: 100.00\n"
    "TAX: 10.00\n"
)


class FakeOcrClient:
    """假 OCR：受控面与 OcrClient 一致（settings/process_file/_load_cache/stats）。"""

    def __init__(self, settings: Settings, text: str = CANNED_OCR_TEXT) -> None:
        self.settings = settings
        self._text = text
        self.stats = {"api_calls": 0, "cache_hits": 0, "failures": 0, "replays": 0}

    def process_file(self, path: Path) -> OcrOutcome:
        return OcrOutcome(text=self._text, confidence=0.99, source="api")

    def _load_cache(self, sha: str) -> None:
        return None


class FakeLlmExtractor:
    """假草稿 LLM：受控面与 LlmExtractor 一致（enabled/extract/stats）。"""

    enabled = True

    def __init__(self, draft: LlmDocument) -> None:
        self._draft = draft
        self.stats = {"calls": 0, "cache_hits": 0, "failures": 0, "repairs": 0}

    def extract(self, text: str, *, filename: str, kind: str) -> LlmDocument:
        self.stats["calls"] += 1
        return self._draft


class FakeAgentLlm:
    """假综合 agent LLM：受控面 .enabled / .complete(prompt) -> str。"""

    enabled = True

    def __init__(self, script) -> None:
        self._script = script
        self.calls = 0

    def complete(self, prompt: str) -> str:
        self.calls += 1
        if callable(self._script):
            return self._script(prompt, self.calls)
        if self._script:
            return self._script.pop(0)
        return json.dumps(
            {"action": "finish", "proposal": {"role": "issuer", "reasons": ["默认"], "confidence": 0.9}}
        )


def _draft(charges: list[LlmCharge], total: float, reference: str = "FAKE-CROSS-001") -> LlmDocument:
    """构造规则草稿为空时的完整 LLM 草稿。"""
    return LlmDocument(
        reference_no=reference,
        document_date="2026-11-01",
        document_type="Debit Note",
        issuer="NEPTUNE SHIPPING LIMITED",
        recipient="ALL AMERICA FORWARDING, INC.",
        currency="USD",
        total_amount=total,
        charges=charges,
        confidence=0.9,
    )


def _finish(role, confidence: float, charge_item: str = "Handling Charge") -> str:
    return json.dumps(
        {"action": "finish", "proposal": {
            "role": role, "charge_item": charge_item,
            "reasons": ["测试提案"], "confidence": confidence,
        }}
    )


def test_audit_trail_carries_quantity_rate_subtotal_tax_snippet(
    settings: Settings, samples_dir: Path
) -> None:
    """audit_trail 补齐数量/单价/小计/税额/原文片段，顶层六字段不变。

    用规则可解析的假 OCR 文本（含 SUBTOTAL/TAX、费用行带数量/单价），
    不注入草稿 LLM 与综合 agent，走确定性链。断言五个新字段都取自草稿
    真实数据（不虚构），且顶层仍是题目要求的六字段。
    """
    agent = BillingAgent(
        settings, ocr_client=FakeOcrClient(settings, text=RULES_TEXT_WITH_TOTALS)
    )
    result = agent.run([samples_dir / "图片示例.png"])

    assert not result.failures
    assert len(result.records) == 1
    record = result.records[0]
    audit = record.audit_trail
    assert audit.quantity == 1.0
    assert audit.unit_rate == 100.0
    assert audit.subtotal == 100.0
    assert audit.tax == 10.0
    assert audit.source_snippet and "HANDLING CHARGE" in audit.source_snippet
    # 顶层六字段契约不变
    assert set(record.model_dump()) == {
        "operation", "document_info", "transaction_party",
        "charge_details", "financial_attributes", "audit_trail",
    }


def _run_agent(settings: Settings, samples_dir: Path, draft: LlmDocument, agent_llm: FakeAgentLlm):
    agent = BillingAgent(
        settings,
        ocr_client=FakeOcrClient(settings),
        llm=FakeLlmExtractor(draft),
        agent_llm=agent_llm,
    )
    result = agent.run([samples_dir / "图片示例.png"])
    assert not result.failures
    return agent, result


def test_agent_wrong_direction_proposal_blocked_by_tail(
    settings: Settings, samples_dir: Path
) -> None:
    """agent 提案被尾巴拦下：方向与四要素规则复查矛盾 → PENDING_REVIEW，不落账。

    记账主体 NEPTUNE 是签发方，四要素死规则判 AR；agent 却提案 role=recipient
    （落出 AP）。尾巴用 decide_posting 复查发现 definite 矛盾，拦下转人工，
    错提案进不了总账。
    """
    charge = LlmCharge(
        raw_name="Handling Fee", amount=88.88, currency="USD",
        evidence="HANDLING FEE USD 88.88",
    )
    agent_llm = FakeAgentLlm([_finish(role="recipient", confidence=0.9)])
    agent, result = _run_agent(settings, samples_dir, _draft([charge], total=88.88), agent_llm)

    assert len(result.records) == 1
    record = result.records[0]
    assert record.operation == "PENDING_REVIEW"
    assert any("方向复查" in reason for reason in record.audit_trail.ai_reasoning)
    # 错提案不落账：台账没有这条新增行
    assert not any(line.reference_no == "FAKE-CROSS-001" for line in agent.ledger.lines)


def test_agent_low_confidence_proposal_blocked_by_tail(
    settings: Settings, samples_dir: Path
) -> None:
    """agent 提案被尾巴拦下：自标置信度过低（不可信）→ PENDING_REVIEW，不落账。"""
    charge = LlmCharge(
        raw_name="Handling Fee", amount=88.88, currency="USD",
        evidence="HANDLING FEE USD 88.88",
    )
    agent_llm = FakeAgentLlm([_finish(role="issuer", confidence=0.2)])
    agent, result = _run_agent(settings, samples_dir, _draft([charge], total=88.88), agent_llm)

    assert len(result.records) == 1
    record = result.records[0]
    assert record.operation == "PENDING_REVIEW"
    assert any("置信度" in reason for reason in record.audit_trail.ai_reasoning)
    assert not any(line.reference_no == "FAKE-CROSS-001" for line in agent.ledger.lines)


def test_amount_recompute_mismatch_blocked_by_tail(
    settings: Settings, samples_dir: Path
) -> None:
    """确定性链同样被尾巴把关：单价×数量≠金额 → PENDING_REVIEW，不落账。

    不注入综合 agent（走确定性入账链）。数量 2 × 单价 10 = 20 ≠ 金额 88.88，
    金额重算（复用 validate_charge）命中 AMOUNT_CALC_MISMATCH，尾巴拦下。
    证明交叉验证对确定性与 agent 两路一致生效。
    """
    charge = LlmCharge(
        raw_name="Handling Fee", amount=88.88, currency="USD",
        quantity=2.0, unit_rate=10.0, evidence="HANDLING FEE USD 88.88",
    )
    agent = BillingAgent(
        settings,
        ocr_client=FakeOcrClient(settings),
        llm=FakeLlmExtractor(_draft([charge], total=88.88)),
    )
    result = agent.run([samples_dir / "图片示例.png"])

    assert not result.failures
    assert len(result.records) == 1
    record = result.records[0]
    assert record.operation == "PENDING_REVIEW"
    assert any("金额复查" in reason for reason in record.audit_trail.ai_reasoning)
    assert not any(line.reference_no == "FAKE-CROSS-001" for line in agent.ledger.lines)
