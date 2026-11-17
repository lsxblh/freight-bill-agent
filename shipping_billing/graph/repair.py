"""解析补救 agent：软失败时自主换策略重读（有限次数），救不回则产出 PENDING_REVIEW。

职责边界（对应 GLOSSARY"解析补救 agent"）：
* 软失败（读出了文字但不够干净）时，自己决定换哪种解析策略，回到"读进来"重走：
  - 文本层空 / OCR 数字混淆 / 置信度低 → 强制重 OCR（``ocr``）；
  - 其余（如裁判判负的边界 case）→ 带反馈重抽（``reextract``）：同样强制
    重 OCR 换路重读，并把闸门原因喂进 LLM 草稿提示（票08），使重试与首次
    真正不同；原因同时留痕运行日志，供审计与人工复核。
* 重试次数由图的条件边把关（≤ settings.ingest_max_retries）；本 agent 只负责
  "选策略"，不自己计数、不落账。
* 重试耗尽或完全读不出 → 由图调用 ``build_pending_review_record`` 产出一条
  PENDING_REVIEW 记录（含原因），不拿垃圾跑下游。

OCR 经注入的客户端回放，离线可验证；不改 OCR 网关契约。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from ..config import Settings
from ..models import (
    AuditTrail,
    ChargeDetails,
    DocumentInfo,
    FinancialAttributes,
    OutputRecord,
    RawDocument,
    TransactionParty,
)
from ..pipeline_utils import RecordAssessment
from .judge import compute_metrics


@dataclass
class RepairPlan:
    """补救策略。

    Attributes:
        strategy: ``ocr``（强制重 OCR）或 ``reextract``（带反馈重抽）。
        reason: 选择该策略的理由（写进运行日志 / 审计）。
    """

    strategy: str
    reason: str


class RepairAgent:
    """解析补救 agent：按代码指标自主选策略。可注入（假 OCR 驱动离线验证）。

    只负责"选策略"，不直接读文件：换路重读由图 ingest 经 load_document
    执行（使用编排器注入的同一 OCR 客户端），离线可回放。
    """

    def __init__(self, ocr_client, settings: Settings) -> None:
        """Args:
            ocr_client: OCR 客户端注入占位（保持主缝构造签名兼容）。重读
                由图 ingest 统一执行，本 agent 不直接读取，故不再留存
                （票08 清理：原 self.ocr 只存不读）。
            settings: 运行配置。
        """
        self.settings = settings

    def plan(self, raw: RawDocument, gate_reason: str) -> RepairPlan:
        """按原件指标自主选补救策略。

        Args:
            raw: 当前（被判软失败的）原件。
            gate_reason: 闸门给出的失败原因，作为重抽反馈。

        Returns:
            补救策略。
        """
        metrics = compute_metrics(raw)
        if metrics.text_chars == 0 and not metrics.has_tables:
            return RepairPlan("ocr", "文本层为空，改走 OCR 重读")
        if metrics.corrupt_amounts or (
            metrics.ocr_confidence is not None
            and metrics.ocr_confidence < self.settings.gate_min_confidence
        ):
            return RepairPlan("ocr", "OCR 数字混淆/置信度低，重 OCR 该件")
        return RepairPlan("reextract", f"带反馈重抽：{gate_reason}")


def build_pending_review_record(
    path: Path, raw: RawDocument | None, reason: str, settings: Settings
) -> tuple[OutputRecord, RecordAssessment]:
    """为"救不回 / 完全读不出"的文件产出一条 PENDING_REVIEW 记录（含原因）。

    没有可抽取的单据/费用，六字段一律 null（不虚构），仅保留记账主体与来源；
    原因写进 audit_trail，供人工复核。同时返回一条对应的 RecordAssessment，
    使汇总的 operations/posting 计数与正常记录一致。

    Args:
        path: 文件路径。
        raw: 原件（完全读不出时为 None）。
        reason: 转人工的原因。
        settings: 运行配置。

    Returns:
        (PENDING_REVIEW 输出记录, 对应处理结论)。
    """
    record = OutputRecord(
        operation="PENDING_REVIEW",
        document_info=DocumentInfo(),
        transaction_party=TransactionParty(),
        charge_details=ChargeDetails(),
        financial_attributes=FinancialAttributes(
            accounting_entity=settings.accounting_entity, posting_type=None
        ),
        audit_trail=AuditTrail(
            ai_reasoning=[f"文件无法可靠解析，转人工复核：{reason}"],
            source={
                "file": path.name,
                "kind": raw.kind if raw else None,
                "ocr_source": raw.ocr_source if raw else None,
                "extractor": "gate",
            },
            confidence=0.0,
            warnings=[reason],
        ),
    )
    assessment = RecordAssessment(
        record=record, operation="PENDING_REVIEW", confidence=0.0, issues=[]
    )
    return record, assessment


__all__ = ["RepairAgent", "RepairPlan", "build_pending_review_record"]
