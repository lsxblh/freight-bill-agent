"""死规则尾巴的钱关键项确定性交叉验证（票05）。

综合 agent 与确定性入账链都经 assemble_record 产出提案（RecordAssessment），
再进入尾巴落账。本模块在落账（_apply_to_ledger）之前对钱关键项做最后一道
确定性复查，**复用现有校验/认亲/过账代码，不新造一套**：

* 金额重算 —— 复用 validation.validators（单价×数量、明细加总、币种）；
* 认亲复查 —— 复用 matching.matcher.match_charge（与认亲工具同源）；
* 方向复查 —— 复用 finance.posting.decide_posting（四要素死规则）；
* 再结合 agent 自标低置信。

任何一项对不上或不可信 → 把提案降级为 PENDING_REVIEW（错提案不落账）。
复查对两条路径一致生效：确定性路径重新推导必然与自身一致（不误伤），
agent 路径若提案与确定性事实矛盾则被拦下。

依赖方向：graph → validation/matching/finance/pipeline_utils，无反向依赖。
"""

from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING

from ..finance.posting import decide_posting
from ..io_utils import get_logger
from ..matching.matcher import match_charge
from ..models import ChargeLine, ExtractedDocument
from ..pipeline_utils import RecordAssessment
from ..validation.validators import validate_charge, validate_document

if TYPE_CHECKING:
    from ..pipeline import BillingAgent

logger = get_logger("crosscheck")

#: 金额重算/币种的关键问题码：命中即"钱对不上或无效"，转人工。
#: 其中 AMOUNT_CALC_MISMATCH/TOTAL_MISMATCH 是 warning 级，上游不阻断，
#: 由尾巴在落账前兜底拦下；AMOUNT_MISSING/CURRENCY_INVALID 本就阻断，
#: 这里独立复核一遍，不依赖上游是否已拦。
_MONEY_ISSUE_CODES = frozenset(
    {"AMOUNT_MISSING", "CURRENCY_INVALID", "AMOUNT_CALC_MISMATCH", "TOTAL_MISMATCH"}
)

#: agent 自标置信度低于此值视为"不可信"（0.5 = agent 自己都没过半把握）。
#: 确定性路径 agent_confidence 为 None，不触发本项。
_AGENT_LOW_CONFIDENCE = 0.5


def crosscheck_assessment(
    owner: BillingAgent,
    assessment: RecordAssessment,
    doc: ExtractedDocument,
    charge: ChargeLine,
) -> RecordAssessment:
    """落账前对提案做钱关键项确定性交叉验证。

    Args:
        owner: 编排器实例（提供活台账与配置）。
        assessment: 综合段产出的提案（尚未落账）。
        doc: 当前费用所属单据草稿。
        charge: 当前费用行。

    Returns:
        全部复查通过 → 原提案不变；任一不一致/不可信 → 降级为
        PENDING_REVIEW 的提案（operation 改写、理由入审计、清空 updates），
        尾巴据此不落账。
    """
    reasons = _rejection_reasons(owner, assessment, doc, charge)
    if not reasons:
        return assessment
    logger.info(
        "尾巴交叉验证拦下提案: %s / %s -> %s",
        doc.effective_reference(),
        charge.charge_item,
        "; ".join(reasons),
    )
    return _downgrade_to_pending(assessment, reasons)


def _rejection_reasons(
    owner: BillingAgent,
    assessment: RecordAssessment,
    doc: ExtractedDocument,
    charge: ChargeLine,
) -> list[str]:
    """逐项确定性复查，返回"对不上/不可信"的理由列表（空 = 全部通过）。"""
    settings = owner.settings
    reasons: list[str] = []

    # 1) 金额重算：复用 validators（单价×数量、明细加总、金额/币种有效性）。
    issues = validate_charge(charge, doc)
    issues.extend(validate_document(doc, tolerance_ratio=settings.total_tolerance_ratio))
    for issue in issues:
        if issue.code in _MONEY_ISSUE_CODES:
            reasons.append(f"金额复查：{issue.message}")

    # 2) 认亲复查：复用四级匹配（与认亲工具同源），对活台账重新认亲，
    #    与提案的匹配结论比对。synthesize 与 tail 之间台账未变，确定性
    #    路径必然一致；此项是防提案篡改匹配结论的兜底。
    recheck = match_charge(charge, doc, owner.ledger, settings)
    claimed_line = assessment.bill_line
    if recheck.matched != (claimed_line is not None):
        reasons.append("认亲复查：与提案的匹配结论不一致")
    elif (
        recheck.matched
        and claimed_line is not None
        and recheck.line is not None
        and recheck.line.bill_id != claimed_line.bill_id
    ):
        reasons.append(
            f"认亲复查：台账行不一致（提案 {claimed_line.bill_id} / 复查 {recheck.line.bill_id}）"
        )

    # 3) 方向复查：复用四要素死规则 decide_posting 重新判方向。仅在
    #    "规则给出确定方向且与提案确定方向矛盾"时拦下；规则自身拿不准
    #    （None/转人工）不构成矛盾，不覆盖 agent 在疑难 case 的推理。
    repost = decide_posting(
        entity=settings.accounting_entity,
        issuer=doc.issuer,
        recipient=doc.recipient,
        document_type=doc.document_type,
        amount_sign=charge.amount_sign,
    )
    claimed_posting = assessment.record.financial_attributes.posting_type
    if (
        repost.posting_type is not None
        and not repost.requires_review
        and claimed_posting is not None
        and repost.posting_type != claimed_posting
    ):
        reasons.append(
            f"方向复查：四要素规则判 {repost.posting_type}，提案为 {claimed_posting}"
        )

    # 4) agent 自标低置信：确定性路径为 None 不触发。
    agent_confidence = assessment.record.audit_trail.agent_confidence
    if agent_confidence is not None and agent_confidence < _AGENT_LOW_CONFIDENCE:
        reasons.append(f"综合 agent 自标置信度 {agent_confidence:.2f} 过低，不可信")

    return reasons


def _downgrade_to_pending(
    assessment: RecordAssessment, reasons: list[str]
) -> RecordAssessment:
    """把提案降级为 PENDING_REVIEW：改写 operation、理由入审计、清空 updates。

    保留 bill_line/field_changes 供人工复核参照；operation 改为 PENDING_REVIEW
    后 _apply_to_ledger 不命中任何落账分支，错提案进不了总账。
    """
    audit = assessment.record.audit_trail
    new_audit = audit.model_copy(
        update={
            "ai_reasoning": [
                *audit.ai_reasoning,
                *(f"尾巴交叉验证拦下：{reason}" for reason in reasons),
            ],
            "warnings": [*audit.warnings, *reasons],
        }
    )
    new_record = assessment.record.model_copy(
        update={"operation": "PENDING_REVIEW", "audit_trail": new_audit}
    )
    return replace(assessment, record=new_record, operation="PENDING_REVIEW", updates={})


__all__ = ["crosscheck_assessment"]
