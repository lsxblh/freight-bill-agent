"""票04：综合 agent（ReAct 工作台）主缝端到端测试。

测试缝（与规格 Testing Decisions 一致，不新增内部缝）：
* 主缝 ``BillingAgent(settings, *, ocr_client, llm, agent_llm).run(paths)``；
* 综合 agent 的 LLM 经新增 ``agent_llm`` 关键字注入假实现，受控面为
  ``.enabled`` / ``.complete(prompt) -> str``，绝不真联网；
* 只断言外部行为：records / operation / audit_trail / ledger。

假 agent LLM 用脚本化的 ReAct 动作驱动工具循环：每个回合返回一个 JSON
动作（调工具或 finish 提案），以此证明 agent 自主决定何时调哪个工具、
提案如何经死规则尾巴把关落账。
"""

from __future__ import annotations

import json
from pathlib import Path

from freight_bill.config import Settings
from freight_bill.extract.llm_extractor import LlmCharge, LlmDocument
from freight_bill.ingest.ocr_client import OcrOutcome
from freight_bill.pipeline import BillingAgent

#: 假 OCR 文本：规则抽不出任何字段，单据内容完全由假 LLM 草稿提供。
CANNED_OCR_TEXT = (
    "Canned OCR text for the comprehensive agent test.\n"
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
    """假草稿 LLM：受控面与 LlmExtractor 一致（enabled/extract/stats）。"""

    enabled = True

    def __init__(self, draft: LlmDocument) -> None:
        self._draft = draft
        self.stats = {"calls": 0, "cache_hits": 0, "failures": 0, "repairs": 0}

    def extract(self, text: str, *, filename: str, kind: str) -> LlmDocument:
        self.stats["calls"] += 1
        return self._draft


class FakeAgentLlm:
    """假综合 agent LLM：受控面 .enabled / .complete(prompt) -> str。

    Args:
        script: 字符串列表（按回合依次弹出）或可调用对象
            ``(prompt, call_index) -> str``。耗尽后默认返回 finish。
    """

    enabled = True

    def __init__(self, script) -> None:
        self._script = script
        self.prompts: list[str] = []
        self.calls = 0

    def complete(self, prompt: str) -> str:
        self.prompts.append(prompt)
        self.calls += 1
        if callable(self._script):
            return self._script(prompt, self.calls)
        if self._script:
            return self._script.pop(0)
        return json.dumps(
            {"action": "finish", "proposal": {"role": "issuer", "reasons": ["默认"], "confidence": 0.9}}
        )


def _draft(charges: list[LlmCharge], total: float, reference: str = "FAKE-AGENT-001") -> LlmDocument:
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


def _run(settings: Settings, samples_dir: Path, draft: LlmDocument, agent_llm: FakeAgentLlm):
    agent = BillingAgent(
        settings,
        ocr_client=FakeOcrClient(settings),
        llm=FakeLlmExtractor(draft),
        agent_llm=agent_llm,
    )
    result = agent.run([samples_dir / "图片示例.png"])
    assert not result.failures
    return agent, result


def test_agent_unknown_role_goes_pending_review(settings: Settings, samples_dir: Path) -> None:
    """综合 agent 判不出我方角色（role=None）：死规则尾巴不猜，转人工。

    这钉住"AR/AP 推理在 agent、角色→AR/AP 落账那一下在死规则"：agent
    给出空角色候选，posting_from_role 不臆造方向，operation 落 PENDING_REVIEW。
    """
    charge = LlmCharge(
        raw_name="Handling Fee", amount=88.88, currency="USD",
        evidence="HANDLING FEE USD 88.88",
    )
    agent_llm = FakeAgentLlm(
        [json.dumps({"action": "finish", "proposal": {
            "role": None, "reasons": ["签发方/接收方均无法确认我方角色"], "confidence": 0.3,
        }})]
    )
    _, result = _run(settings, samples_dir, _draft([charge], total=88.88), agent_llm)

    assert len(result.records) == 1
    record = result.records[0]
    assert record.operation == "PENDING_REVIEW"
    assert record.financial_attributes.posting_type is None
    # agent 的理由与自标置信度写入 audit_trail
    assert record.audit_trail.agent_role is None
    assert record.audit_trail.agent_confidence == 0.3
    assert any("综合 agent" in reason for reason in record.audit_trail.ai_reasoning)


def test_agent_calls_match_tool_and_feeds_observation_back(
    settings: Settings, samples_dir: Path
) -> None:
    """综合 agent 自主调认亲工具，工具事实回喂下一回合，提案经尾巴落账。

    钉住 ReAct 工作台：第一回合调 match_charge（确定性四级匹配工具），
    观察（认没认出/哪条/第几级）回喂进第二回合提示词，agent 据此 finish。
    认亲事实本身由确定性函数给出，agent 只消费、不臆造。
    """
    charge = LlmCharge(
        raw_name="Handling Fee", amount=88.88, currency="USD",
        evidence="HANDLING FEE USD 88.88",
    )
    agent_llm = FakeAgentLlm(
        [
            json.dumps({"action": "match_charge", "args": {}}),
            json.dumps({"action": "finish", "proposal": {
                "role": "issuer", "charge_item": "Handling Charge",
                "reasons": ["认亲工具确认无历史账单，按新增；我方为签发方故应收"],
                "confidence": 0.9,
            }}),
        ]
    )
    _, result = _run(settings, samples_dir, _draft([charge], total=88.88), agent_llm)

    # 两个回合：先调工具，再据观察产提案
    assert agent_llm.calls == 2
    # 第二回合提示词里含第一回合的认亲观察事实（matched=false → 新账单）
    assert "match_charge" in agent_llm.prompts[1]
    assert "观察" in agent_llm.prompts[1]
    assert '"matched": false' in agent_llm.prompts[1]

    assert len(result.records) == 1
    record = result.records[0]
    assert record.operation == "CREATE"
    assert record.financial_attributes.posting_type == "AR"
    assert record.charge_details.charge_item == "Handling Charge"
    # 未认亲 → 无匹配规则；agent 角色/置信度/理由入审计
    assert record.audit_trail.match_rule is None
    assert record.audit_trail.agent_role == "issuer"
    assert record.audit_trail.agent_confidence == 0.9
    assert any("综合 agent" in reason for reason in record.audit_trail.ai_reasoning)


def test_agent_autonomously_uses_dictionary_raw_and_ledger_tools(
    settings: Settings, samples_dir: Path
) -> None:
    """综合 agent 自主决定查字典/读原文/查台账，三个工具事实都回喂进上下文。"""
    charge = LlmCharge(
        raw_name="Handling Fee", amount=88.88, currency="USD",
        evidence="HANDLING FEE USD 88.88",
    )
    agent_llm = FakeAgentLlm(
        [
            json.dumps({"action": "lookup_charge_name", "args": {"raw_name": "Handling Fee"}}),
            json.dumps({"action": "read_raw", "args": {"keyword": "HANDLING"}}),
            json.dumps({"action": "query_ledger", "args": {"charge_item": "Handling Charge"}}),
            json.dumps({"action": "finish", "proposal": {
                "role": "issuer", "charge_item": "Handling Charge",
                "reasons": ["字典收录 Handling Fee→Handling Charge；原文与台账核对后按新增"],
                "confidence": 0.9,
            }}),
        ]
    )
    _, result = _run(settings, samples_dir, _draft([charge], total=88.88), agent_llm)

    assert agent_llm.calls == 4
    # 最后一回合提示词累积了三个工具的观察事实
    last_prompt = agent_llm.prompts[3]
    assert '"known": true' in last_prompt          # 字典：收录
    assert "Handling Charge" in last_prompt         # 字典：标准名
    assert "Canned OCR" in last_prompt              # 读原文：原件片段
    assert '"count"' in last_prompt                 # 查台账：行数事实

    assert len(result.records) == 1
    record = result.records[0]
    assert record.operation == "CREATE"
    assert record.charge_details.charge_item == "Handling Charge"
    assert record.audit_trail.agent_role == "issuer"


def test_agent_runs_without_llm_draft(settings: Settings, samples_dir: Path) -> None:
    """LLM 草稿可选：不注入草稿 LLM（仅规则草稿），综合 agent 仍正常跑。

    用真实 OCR 缓存回放的图片样例（规则可抽出费用），llm=None，只注入
    假综合 agent LLM。证明综合段不依赖第2段 LLM 草稿（读失败留空不阻断）。
    """
    agent_llm = FakeAgentLlm([])  # 耗尽后默认 finish(role=issuer)
    agent = BillingAgent(settings, agent_llm=agent_llm)
    result = agent.run([samples_dir / "图片示例.png"])

    assert not result.failures
    assert len(result.records) == 2
    # 两条费用都经过综合 agent（角色候选入审计），且未请求草稿 LLM
    assert all(r.audit_trail.agent_role == "issuer" for r in result.records)
    assert agent.llm.enabled is False


def test_agent_preserves_per_charge_ledger_interleave(
    settings: Settings, samples_dir: Path
) -> None:
    """agent 激活下仍逐条"提案→落账"：同名两条费用 CREATE→UPDATE 交错。

    这是票02 行为钉子在综合 agent 路径上的复现：认亲读活台账，第一条
    CREATE 落账后第二条才能认亲它。若改成整单批量提案再落账，两条都会
    变 CREATE——行为就变了。
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
    agent_llm = FakeAgentLlm(
        lambda prompt, call: json.dumps({"action": "finish", "proposal": {
            "role": "issuer", "charge_item": "Handling Charge",
            "reasons": ["我方为签发方，应收"], "confidence": 0.9,
        }})
    )
    _, result = _run(settings, samples_dir, draft, agent_llm)

    assert [r.operation for r in result.records] == ["CREATE", "UPDATE"]
    assert result.records[1].audit_trail.match_rule == "单据编号+费用名称"
    assert [c["field"] for c in result.records[1].audit_trail.field_changes] == ["amount"]
    # 两条都经综合 agent 逐条提案（各 finish 一次）
    assert agent_llm.calls == 2
    assert all(r.audit_trail.agent_role == "issuer" for r in result.records)


def test_agent_keeps_unknown_charge_name_no_fabrication(
    settings: Settings, samples_dir: Path
) -> None:
    """字典权威：未收录费用名经 agent 查证后仍保留原名，不臆造标准名。"""
    charge = LlmCharge(
        raw_name="Mystery Surcharge XYZ", amount=50.0, currency="USD",
        evidence="MYSTERY SURCHARGE XYZ USD 50.00",
    )
    agent_llm = FakeAgentLlm(
        [
            json.dumps({"action": "lookup_charge_name", "args": {"raw_name": "Mystery Surcharge XYZ"}}),
            json.dumps({"action": "finish", "proposal": {
                "role": "issuer", "charge_item": "Mystery Surcharge XYZ",
                "reasons": ["字典未收录该费用名，保留原名并降低置信度，不臆造"],
                "confidence": 0.5,
            }}),
        ]
    )
    _, result = _run(settings, samples_dir, _draft([charge], total=50.0), agent_llm)

    # 字典工具事实回喂：known=false（未收录）
    assert '"known": false' in agent_llm.prompts[1]
    assert len(result.records) == 1
    record = result.records[0]
    # 输出保留原名（字典权威，未收录不臆造），低置信入审计
    assert record.charge_details.charge_item == "Mystery Surcharge XYZ"
    assert record.audit_trail.agent_confidence == 0.5
    assert record.operation == "CREATE"
