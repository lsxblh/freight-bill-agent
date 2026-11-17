"""票06：综合 agent 必需 key（命脉模式）主缝测试。

测试缝（与规格 Testing Decisions 一致）：
* 主缝 ``BillingAgent(settings, *, agent_llm=...)`` 的构造与 run；
* 配置缝 ``Settings.from_env``（环境变量 AGENT_REQUIRED_KEY）。

命脉模式（settings.agent_required_key=True，规格 Q5 已批准的行为变更）：
综合 agent 必须可用——无 key 且无注入时构造即明确报错，绝不静默降级
到确定性链；配了 key 则默认启用真实适配器 ChatAgentLlm。兼容模式
（缺省 False）保持票04 行为：agent_llm 缺省 None 走确定性链。

全部离线：注入假 agent LLM 或只验构造，绝不真联网。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from freight_bill.config import Settings
from freight_bill.graph.agents import ChatAgentLlm
from freight_bill.pipeline import BillingAgent, MissingAgentKeyError


class FakeAgentLlm:
    """假综合 agent LLM：受控面 .enabled / .complete(prompt) -> str。"""

    def __init__(self, enabled: bool = True) -> None:
        self.enabled = enabled
        self.calls = 0

    def complete(self, prompt: str) -> str:
        self.calls += 1
        return json.dumps(
            {"action": "finish", "proposal": {
                "role": "issuer", "reasons": ["测试提案"], "confidence": 0.9,
            }}
        )


def test_required_key_missing_raises_at_construction(settings: Settings) -> None:
    """命脉模式无 key 无注入：构造即报错，错误信息点名 LLM_API_KEY。

    钉住"无 API key 明确报错、不静默降级"：错误发生在构造期（fail-fast），
    不是跑完一批才发现悄悄走了确定性链。
    """
    settings = settings.with_overrides(agent_required_key=True)
    with pytest.raises(MissingAgentKeyError) as excinfo:
        BillingAgent(settings)
    message = str(excinfo.value)
    assert "LLM_API_KEY" in message
    assert "AGENT_REQUIRED_KEY" in message


def test_required_key_with_key_auto_enables_real_adapter(settings: Settings) -> None:
    """命脉模式配了 key：未注入时默认启用 ChatAgentLlm（综合 agent 命脉）。

    只验构造：ChatAgentLlm 懒构造模型，构造期不联网；.enabled 为 True
    证明综合段会走 ReAct 而非确定性链。
    """
    settings = settings.with_overrides(
        agent_required_key=True, llm_enabled=True, llm_api_key="sk-test-offline"
    )
    agent = BillingAgent(settings)
    assert isinstance(agent.agent_llm, ChatAgentLlm)
    assert agent.agent_llm.enabled is True


def test_required_mode_injected_enabled_agent_runs_offline(
    settings: Settings, samples_dir: Path
) -> None:
    """命脉模式注入启用的假 agent LLM：离线端到端正常走 ReAct 提案。

    用真实 OCR 缓存回放（不联网），证明命脉模式不破坏主缝注入路径，
    且综合段确实经过 agent（角色候选入审计）。
    """
    settings = settings.with_overrides(agent_required_key=True)
    fake = FakeAgentLlm(enabled=True)
    agent = BillingAgent(settings, agent_llm=fake)
    result = agent.run([samples_dir / "图片示例.png"])

    assert not result.failures
    assert len(result.records) == 2
    assert fake.calls == 2
    assert all(r.audit_trail.agent_role == "issuer" for r in result.records)


def test_required_mode_injected_disabled_agent_raises(settings: Settings) -> None:
    """命脉模式注入禁用的 agent LLM：同样报错，不允许静默降级。

    注入但 .enabled=False 等价于"没有可用的综合 agent"——若放行，
    synthesize 会悄悄回落确定性链，正是 Q5 禁止的静默降级。
    """
    settings = settings.with_overrides(agent_required_key=True)
    with pytest.raises(MissingAgentKeyError):
        BillingAgent(settings, agent_llm=FakeAgentLlm(enabled=False))


def test_from_env_reads_agent_required_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """配置缝：from_env（CLI/生产入口）缺省命脉模式，显式 false 可回退。

    拍板 Q1=A：生产入口缺省 true（无 key 明确报错，体现命脉定位）；
    Settings 数据类缺省仍 False（程序化构造/离线测试不受影响）。
    """
    monkeypatch.delenv("AGENT_REQUIRED_KEY", raising=False)
    assert Settings.from_env().agent_required_key is True
    monkeypatch.setenv("AGENT_REQUIRED_KEY", "false")
    assert Settings.from_env().agent_required_key is False
    monkeypatch.setenv("AGENT_REQUIRED_KEY", "true")
    assert Settings.from_env().agent_required_key is True


def test_compat_mode_defaults_to_deterministic(settings: Settings) -> None:
    """兼容模式（缺省）：不注入则 agent_llm=None，保持票04 确定性行为。"""
    agent = BillingAgent(settings)
    assert agent.agent_llm is None


def test_cli_missing_key_exits_with_clear_error(
    monkeypatch: pytest.MonkeyPatch, samples_dir: Path, capsys: pytest.CaptureFixture
) -> None:
    """CLI 命脉模式无 key：退出码 2 + 点名 LLM_API_KEY 的报错，不抛裸栈。"""
    from freight_bill.cli import main

    monkeypatch.setenv("AGENT_REQUIRED_KEY", "true")
    monkeypatch.delenv("LLM_API_KEY", raising=False)
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setenv("LLM_ENABLED", "false")
    code = main(
        ["run", "--inputs", str(samples_dir), "--ocr-mode", "cache", "--quiet"]
    )
    assert code == 2
    assert "LLM_API_KEY" in capsys.readouterr().err


def test_eval_gate_stays_offline_deterministic_without_key(
    monkeypatch: pytest.MonkeyPatch, samples_dir: Path, tmp_path: Path
) -> None:
    """eval 是 7/7 回归闸门：显式钉住离线确定性，命脉模式无 key 也能跑。

    拍板 Q2：闸门必须可复现——cmd_eval 内部钉住兼容模式 + 关闭 LLM，
    不受 AGENT_REQUIRED_KEY/LLM_ENABLED 环境变量影响，绝不联网。
    """
    from freight_bill.cli import main

    monkeypatch.setenv("AGENT_REQUIRED_KEY", "true")
    monkeypatch.delenv("LLM_API_KEY", raising=False)
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setenv("LLM_ENABLED", "false")
    out_dir = tmp_path / "eval_gate"
    code = main(
        ["eval", "--inputs", str(samples_dir), "--ocr-mode", "cache",
         "--out-dir", str(out_dir), "--quiet"]
    )
    assert code == 0
    payload = json.loads((out_dir / "eval_report.json").read_text(encoding="utf-8"))
    assert payload["matched"] == 7
    assert payload["recall"] == 1.0
    assert payload["precision"] == 1.0
    assert payload["operation_accuracy"] == 1.0
    assert payload["posting_accuracy"] == 1.0
