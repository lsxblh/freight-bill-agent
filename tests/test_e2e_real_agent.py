"""票06：真实端到端测试（真模型联网，显式启用，拍板 Q2）。

离线确定性 eval 是 7/7 回归闸门（cmd_eval 已钉住）；本测试是"真实端到端"
证据：完整 BillingAgent.run 跑全部样例，综合 agent 用真 ChatAgentLlm
（真 key、联网），OCR 仍走 cache 回放（不依赖网关上传）。

运行方式（默认离线套件不含本测试）：
    先设环境变量 LLM_API_KEY（可选 LLM_BASE_URL / LLM_MODEL）
    python -m pytest tests/test_e2e_real_agent.py -m e2e

无 key 时优雅 skip。断言容忍真模型非确定性：宽松 recall/precision 阈值 +
综合 agent 确实激活 + 批次无失败，**不是 7/7 硬闸门**。
key 只从环境变量读取，绝不写入仓库、提交或日志。
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from shipping_billing.config import PROJECT_ROOT, Settings
from shipping_billing.harness.evaluation import evaluate, load_expectations
from shipping_billing.pipeline import BillingAgent

SAMPLES = PROJECT_ROOT / "data" / "samples"
LEDGER = PROJECT_ROOT / "data" / "ledger" / "existing_bills.json"
OCR_CACHE = PROJECT_ROOT / "data" / "ocr_cache"
GOLDEN = PROJECT_ROOT / "data" / "expectations" / "golden.json"

_HAS_KEY = bool(os.getenv("LLM_API_KEY"))

pytestmark = [
    pytest.mark.e2e,
    pytest.mark.skipif(not _HAS_KEY, reason="未设置 LLM_API_KEY，跳过真模型端到端"),
]

#: 宽松阈值：容忍真模型非确定性（清晰样例应全对，疑难 case 允许波动）。
_MIN_RECALL = 0.8
_MIN_PRECISION = 0.8


def test_real_agent_end_to_end(tmp_path: Path) -> None:
    """真 key 全链路：读进来(cache)→双路抽取→真综合 agent→死规则尾巴。"""
    settings = Settings(
        ocr_mode="cache",
        ledger_path=LEDGER,
        ocr_cache_dir=OCR_CACHE,
        accounting_entity="NEPTUNE SHIPPING LIMITED",
        entity_aliases=("NEPTUNE SHIPPING LIMITED (LAX)",),
        work_dir=tmp_path / "work",
        output_dir=tmp_path / "out",
        llm_cache_dir=tmp_path / "llm",
        # 命脉模式 + 真 key：BillingAgent 自动启用 ChatAgentLlm（不注入假实现）
        agent_required_key=True,
        llm_enabled=True,
        llm_api_key=os.environ["LLM_API_KEY"],
        llm_base_url=os.getenv("LLM_BASE_URL", "https://api.deepseek.com/v1"),
        llm_model=os.getenv("LLM_MODEL", "deepseek-chat"),
    )
    agent = BillingAgent(settings)
    result = agent.run([SAMPLES])

    # 硬契约：批次无失败；样例正常产 7 条记录，容忍真模型小幅波动
    assert not result.failures
    assert len(result.records) >= 6
    # 综合 agent 确实激活（每条都经 ReAct 提案，而非静默回落确定性链）
    assert all(r.audit_trail.agent_confidence is not None for r in result.records)

    cases = load_expectations(GOLDEN)
    payload = evaluate(result.records, cases).to_dict()
    brief = {k: payload[k] for k in (
        "total_expected", "matched", "recall", "precision",
        "operation_accuracy", "posting_accuracy",
    )}
    print("\n[e2e 真模型指标] " + json.dumps(brief, ensure_ascii=False))
    if payload["mismatches"]:
        print("[e2e 差异] " + json.dumps(payload["mismatches"], ensure_ascii=False))
    # 指标落盘：pytest 通过时丢弃捕获的 stdout，落盘便于运行后查阅回报。
    (tmp_path / "e2e_metrics.json").write_text(
        json.dumps({"brief": brief, "mismatches": payload["mismatches"]},
                   ensure_ascii=False, indent=2), encoding="utf-8")

    # 宽松阈值（非 7/7 硬闸门）：真模型质量证据，波动可接受
    assert payload["recall"] >= _MIN_RECALL
    assert payload["precision"] >= _MIN_PRECISION
