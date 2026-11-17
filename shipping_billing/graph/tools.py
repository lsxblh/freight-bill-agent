"""综合 agent 工具层（票04）：确定性"事实"工作台。

四个工具全部由现有模块包一层，只返回事实、不做决策：

* ``match_charge`` —— 认亲（四级匹配，matching.matcher），返回"认没认出 /
  哪条 / 第几级"；
* ``read_raw`` —— 读原文（RawDocument），返回原文或关键词附近片段；
* ``query_ledger`` —— 查台账（Ledger），返回按记账主体隔离后的台账行；
* ``lookup_charge_name`` —— 查费用名字典（normalize.charge_names），返回
  字典权威标准化事实（未收录则 known=False）。

决策（认哪条、判什么角色）由综合 agent 提议、死规则尾巴把关落账。工具
本身可复现：同输入同结果，agent 只消费事实，不臆造。
"""

from __future__ import annotations

from typing import Any

from ..config import Settings
from ..matching.ledger import BillLine, Ledger
from ..matching.matcher import match_charge
from ..models import ChargeLine, ExtractedDocument, RawDocument
from ..normalize.charge_names import standardize

#: 查台账一次最多返回的行数，避免把整本台账塞进模型上下文。
_MAX_LEDGER_LINES = 20
#: 读原文一次最多返回的字符数。
_MAX_RAW_CHARS = 2000


def _line_fact(line: BillLine) -> dict[str, Any]:
    """把一条台账行压成可供模型推理的事实字段。"""
    return {
        "bill_id": line.bill_id,
        "line_id": line.identity(),
        "charge_item": line.charge_item,
        "amount": line.amount,
        "currency": line.currency,
        "reference_no": line.reference_no,
        "document_type": line.document_type,
        "document_date": line.document_date,
        "issuer": line.issuer,
        "recipient": line.recipient,
        "posting_type": line.posting_type,
    }


class AgentTools:
    """绑定"当前费用 + 单据 + 原件 + 台账 + 配置"的工具集（每条费用一套）。

    Args:
        ledger: 活台账（认亲读它，先落账的行可被后条认亲）。
        settings: 运行配置（记账主体、金额容差等）。
        raw: 当前文件的原件；读失败时为 None。
        doc: 当前费用所属单据（规则+LLM 融合草稿）。
        charge: 当前正在综合的费用行。
    """

    def __init__(
        self,
        *,
        ledger: Ledger,
        settings: Settings,
        raw: RawDocument | None,
        doc: ExtractedDocument,
        charge: ChargeLine,
    ) -> None:
        self.ledger = ledger
        self.settings = settings
        self.raw = raw
        self.doc = doc
        self.charge = charge

    # ------------------------------------------------------------------ #
    # 四个确定性工具
    # ------------------------------------------------------------------ #
    def match_charge(self) -> dict[str, Any]:
        """认亲：四级匹配，返回"认没认出 / 哪条 / 第几级"的事实。"""
        result = match_charge(self.charge, self.doc, self.ledger, self.settings)
        line = result.line
        return {
            "matched": result.matched,
            "bill_id": line.bill_id if line is not None else None,
            "line_id": line.identity() if line is not None else None,
            "rule": result.rule,
            "rule_label": result.rule_label,
            "score": result.score,
            "candidate_count": result.candidate_count,
            "conflicts": list(result.conflicts),
            "notes": list(result.notes),
            "matched_line": _line_fact(line) if line is not None else None,
        }

    def read_raw(self, *, keyword: str | None = None) -> dict[str, Any]:
        """读原文：无关键词返回开头片段，有关键词返回其附近窗口。"""
        text = self.raw.text if self.raw is not None else ""
        snippet = text[:_MAX_RAW_CHARS]
        if keyword and keyword in text:
            start = max(0, text.index(keyword) - _MAX_RAW_CHARS // 2)
            snippet = text[start : start + _MAX_RAW_CHARS]
        return {
            "filename": self.raw.filename if self.raw is not None else None,
            "kind": self.raw.kind if self.raw is not None else None,
            "ocr_source": self.raw.ocr_source if self.raw is not None else None,
            "keyword": keyword,
            "text": snippet,
            "truncated": len(text) > len(snippet),
        }

    def query_ledger(
        self, *, reference: str | None = None, charge_item: str | None = None
    ) -> dict[str, Any]:
        """查台账：按记账主体隔离，可按单号/费用名过滤。"""
        if reference or charge_item:
            lines = self.ledger.candidate_lines(
                reference_no=reference,
                charge_item=charge_item,
                entity=self.settings.accounting_entity,
            )
        else:
            lines = self.ledger.scope_lines(self.settings.accounting_entity)
        return {
            "entity": self.settings.accounting_entity,
            "count": len(lines),
            "lines": [_line_fact(line) for line in lines[:_MAX_LEDGER_LINES]],
        }

    def lookup_charge_name(self, *, raw_name: str | None = None) -> dict[str, Any]:
        """查费用名字典：字典权威，未收录 known=False（保留原名+低置信）。"""
        name = raw_name if raw_name is not None else self.charge.raw_name
        result = standardize(name)
        return {
            "original": result.original,
            "standard": result.standard,
            "matched_alias": result.matched_alias,
            "known": result.known,
            "confidence": result.confidence,
        }

    # ------------------------------------------------------------------ #
    def dispatch(self, name: str, args: dict[str, Any] | None) -> dict[str, Any]:
        """按工具名分发一次调用，返回事实 dict（未知工具返回错误事实）。"""
        args = args or {}
        if name == "match_charge":
            return self.match_charge()
        if name == "read_raw":
            return self.read_raw(keyword=args.get("keyword"))
        if name == "query_ledger":
            return self.query_ledger(
                reference=args.get("reference"), charge_item=args.get("charge_item")
            )
        if name == "lookup_charge_name":
            return self.lookup_charge_name(raw_name=args.get("raw_name"))
        return {"error": f"未知工具: {name}"}


__all__ = ["AgentTools"]
