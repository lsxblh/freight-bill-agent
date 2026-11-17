"""票09：综合 agent 装配异常回落的审计留痕回归。

测试缝（与 tests/test_comprehensive_agent.py 一致）：主缝
``BillingAgent(settings, *, agent_llm).run(paths)``，OCR 走 cache 回放，
综合 agent LLM 注入假实现，绝不真联网。

钉住的行为：agent 装配阶段抛异常时，整条费用回落确定性入账链
（record_chain），且 audit_trail.warnings 追加留痕——生产上能区分
"真 agent 提案"与"静默回落"（additive，不改顶层六字段）。

触发方式说明：假 LLM 的 complete() 直接抛异常会被 ReAct 循环内部捕获
（回落确定性提案，仍走 agent 装配路径），到不了装配层回落；因此让假
LLM 返回畸形 finish 提案（reasons 是数字不是列表），提案解析在循环外
抛 TypeError，才命中 synthesize_charge 的装配异常回落分支。
"""

from __future__ import annotations

import json
from pathlib import Path

from freight_bill.config import Settings
from freight_bill.pipeline import BillingAgent


class FakeAgentLlm:
    """假综合 agent LLM：受控面 .enabled / .complete(prompt) -> str。

    Args:
        response: 每次调用都返回的固定响应（本测试用畸形提案）。
    """

    enabled = True

    def __init__(self, response: str) -> None:
        self._response = response
        self.calls = 0

    def complete(self, prompt: str) -> str:
        self.calls += 1
        return self._response


def test_agent_assembly_exception_falls_back_with_audit_trace(
    settings: Settings, samples_dir: Path
) -> None:
    """装配异常 → 回落确定性链 + warnings 留痕，记录仍正常产出。

    图片样例是 ZIM 发票（双方均非记账主体）：确定性链判 PENDING_REVIEW；
    若走 agent 路径（畸形提案里 role=issuer）则会落 CREATE。断言
    PENDING_REVIEW 即证明输出确实来自确定性回落链，而非 agent 装配。
    """
    malformed = json.dumps(
        {"action": "finish", "proposal": {"role": "issuer", "reasons": 42, "confidence": 0.9}}
    )
    agent_llm = FakeAgentLlm(malformed)
    agent = BillingAgent(settings, agent_llm=agent_llm)
    result = agent.run([samples_dir / "图片示例.png"])

    assert not result.failures
    assert len(result.records) == 2
    # 两条费用都尝试过 agent 路径（各调用一次 LLM）
    assert agent_llm.calls == 2
    for record in result.records:
        # 确定性链的结果：ZIM 发票双方非我方 → 转人工，不臆造方向
        assert record.operation == "PENDING_REVIEW"
        assert record.financial_attributes.posting_type is None
        # 未经 agent 装配：角色/自标置信度为空
        assert record.audit_trail.agent_role is None
        assert record.audit_trail.agent_confidence is None
        # 回落留痕：warnings 里能看出"装配异常回落"，不再是静默回落
        assert "agent 装配异常，回落确定性链" in record.audit_trail.warnings