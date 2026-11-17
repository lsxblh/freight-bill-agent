"""LangChain 编排层。

虽然是"确定性优先"的系统，但整条处理链仍然用 LangChain 的 Runnable 组合：

    RunnableParallel(规则抽取 ∥ LLM 抽取)  →  融合  →  标准化  →  校验

好处：
* 两个抽取通道共享同一份输入快照，互不阻塞；
* 每个节点都是纯函数，可单测、可回放、可在中间插入缓存/重试;
* 需要换模型、加检索、加工具时，只改图不改业务代码。

依赖方向（票01 起单向）：本模块只依赖抽取层，抽取层不再反向 import
本模块。单据抽取链 build_document_chain 已移入
extract.document_extractor，这里仅保留明细入账链与兼容再导出。
"""

from __future__ import annotations

from typing import Any

# 兼容再导出：单据抽取链的唯一定义在抽取层，避免编排层↔抽取器循环依赖。
from .extract.document_extractor import build_document_chain


def build_record_chain(context: dict[str, Any]):
    """明细入账链：匹配 → 记账方向 → 校验 → 输出记录。

    context 里放台账、配置等"只读依赖"，避免节点内部再取全局状态。
    """
    from langchain_core.runnables import RunnableLambda

    from .finance.posting import decide_posting
    from .matching.matcher import match_charge
    from .pipeline_utils import assemble_record
    from .validation.validators import validate_charge

    ledger = context["ledger"]
    settings = context["settings"]

    def _match(payload: dict) -> dict:
        charge = payload["charge"]
        doc = payload["document"]
        result = match_charge(charge, doc, ledger, settings)
        return {**payload, "match": result}

    def _posting(payload: dict) -> dict:
        doc = payload["document"]
        decision = decide_posting(
            entity=settings.accounting_entity,
            issuer=doc.issuer,
            recipient=doc.recipient,
            document_type=doc.document_type,
            amount_sign=payload["charge"].amount_sign,
        )
        return {**payload, "posting": decision}

    def _validate(payload: dict) -> dict:
        issues = validate_charge(payload["charge"], payload["document"])
        issues.extend(payload.get("doc_issues") or [])
        return {**payload, "issues": issues}

    return (
        RunnableLambda(_match)
        | RunnableLambda(_posting)
        | RunnableLambda(_validate)
        | RunnableLambda(assemble_record)
    )


__all__ = ["build_document_chain", "build_record_chain"]
