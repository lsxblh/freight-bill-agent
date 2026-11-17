"""综合 agent（票04）：ReAct 工作台，集中"理解与提议"。

第三段 synthesize 的实现。综合 agent 拿融合草稿（规则+LLM）+原件+台账+
记账主体，以 ReAct 工具循环自主决定何时调哪个工具（认亲/读原文/查台账/
查字典），产出提案（角色候选/标准名/理由/置信度）。**只提议不落账**：
提案经死规则装配（assemble_record）与尾巴把关后才进台账。

两条策略：
* 注入了启用的 agent LLM → ReAct 循环（本模块主体）；
* 未注入 / 未启用 → 沿用确定性入账链（record_chain），行为与票02 一致。
  "综合 agent 必须配 key、无 key 报错"是票06 的行为变更，不在本票。

四级匹配始终由确定性函数给出（工具与装配同源），保证认亲可复现；
"角色→AR/AP"那一下用 posting_from_role 死规则，agent 只负责判角色。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from ..config import Settings
from ..finance.posting import PostingDecision, decide_posting, posting_from_role
from ..io_utils import get_logger
from ..matching.matcher import MatchResult, match_charge
from ..models import ChargeLine, ExtractedDocument, RawDocument
from ..pipeline_utils import RecordAssessment, assemble_record
from ..validation.validators import Issue, validate_charge
from .tools import AgentTools

if TYPE_CHECKING:
    from ..pipeline import BillingAgent

logger = get_logger("agent")

#: ReAct 循环回合上限（4 个工具 + finish 绰绰有余；超限回落确定性推理）。
MAX_ROUNDS = 6

SYSTEM_PROMPT = """你是航运智能账单助手的"综合 agent"，坐在工作台前负责"理解与提议"。
你拿到规则+LLM 融合草稿、原件、台账与记账主体，通过工具循环为当前费用行产出一份提案。
你只提议、不落账；落账由死规则尾巴把关。

可用工具（全部返回确定性事实，同输入同结果）：
- match_charge：认亲（四级匹配）。无参数。返回 matched/bill_id/rule_label/conflicts/notes。
- read_raw：读原文。参数 keyword（可选）。返回原文片段。
- query_ledger：查台账。参数 reference/charge_item（可选）。返回按记账主体隔离的台账行。
- lookup_charge_name：查费用名字典。参数 raw_name（可选，默认当前费用）。返回 standard/known/confidence。

每一回合只输出一个 JSON 对象，不要输出任何其它文字：
- 调工具：{"action": "<工具名>", "args": {...}}
- 产提案：{"action": "finish", "proposal": {"role": "issuer"|"recipient"|null, "charge_item": "<标准名>", "reasons": ["..."], "confidence": 0.0~1.0}}

准则：
- 认亲以 match_charge 工具事实为准，你只推理疑难 case，不臆造台账行。
- 费用名字典权威；未收录保留原名并降低置信度，不臆造标准名。
- AR/AP 必须结合签发方/接收方/记账主体/收费方向四要素；判不出我方角色就把 role 置 null（尾巴会转人工），绝不猜。
- 每个判断都要带理由与置信度。"""


@dataclass
class Proposal:
    """综合 agent 对一条费用的提案（建议 + 理由 + 置信度 + 确定性事实）。

    Attributes:
        role: 我方角色候选 issuer/recipient；None 表示判不出（尾巴转人工）。
        charge_item:  reconciled 标准名（字典权威，装配仍以草稿标准名为准）。
        standardized: 费用名是否被字典收录。
        reasons: agent 的推理理由，写入 audit_trail.ai_reasoning。
        confidence: agent 自标置信度，写入 audit_trail.agent_confidence。
        match: 确定性四级匹配结果（与认亲工具同源）。
        posting: 由角色候选经死规则落出的记账方向。
    """

    role: str | None
    charge_item: str
    standardized: bool
    reasons: list[str] = field(default_factory=list)
    confidence: float = 0.5
    match: MatchResult = field(default_factory=MatchResult)
    posting: PostingDecision | None = None


def _clamp_confidence(value: Any) -> float:
    """把模型给的置信度收敛到 [0, 1]，非法值回落 0.5。"""
    try:
        return round(max(0.0, min(1.0, float(value))), 2)
    except (TypeError, ValueError):
        return 0.5


def _parse_action(text: str) -> dict[str, Any] | None:
    """从模型输出里解析出一个 JSON 动作对象（容忍 ```json 围栏与前后噪声）。"""
    if not text:
        return None
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.strip("`")
        if cleaned.lower().startswith("json"):
            cleaned = cleaned[4:]
    start, end = cleaned.find("{"), cleaned.rfind("}")
    if start == -1 or end == -1 or end <= start:
        return None
    try:
        obj = json.loads(cleaned[start : end + 1])
    except (ValueError, TypeError):
        return None
    return obj if isinstance(obj, dict) else None


class ComprehensiveAgent:
    """ReAct 工作台：自主调工具 → 产提案。LLM 经主缝注入，测试用假实现。"""

    def __init__(self, llm: Any, *, max_rounds: int = MAX_ROUNDS) -> None:
        self.llm = llm
        self.max_rounds = max_rounds

    # ------------------------------------------------------------------ #
    def propose(
        self,
        doc: ExtractedDocument,
        charge: ChargeLine,
        *,
        tools: AgentTools,
        settings: Settings,
        doc_issues: list[Issue],
        duplicate_of: str | None,
    ) -> Proposal:
        """对当前费用产出一份提案（只提议不落账）。"""
        raw = self._react_loop(doc, charge, tools, settings, doc_issues, duplicate_of)
        if raw is None:
            return self._deterministic_proposal(doc, charge, tools, settings)

        role = raw.get("role")
        if role not in {"issuer", "recipient"}:
            role = None
        reasons = [str(r) for r in (raw.get("reasons") or [])]
        confidence = _clamp_confidence(raw.get("confidence", 0.5))
        charge_item = str(raw.get("charge_item") or charge.charge_item)

        # 认亲与方向都是确定性事实：认亲同源四级匹配，方向由角色经死规则落出。
        match = match_charge(charge, doc, tools.ledger, settings)
        posting = posting_from_role(
            role=role,
            entity=settings.accounting_entity,
            issuer=doc.issuer,
            recipient=doc.recipient,
            document_type=doc.document_type,
            amount_sign=charge.amount_sign,
        )
        return Proposal(
            role=role,
            charge_item=charge_item,
            standardized=charge.standardized,
            reasons=reasons,
            confidence=confidence,
            match=match,
            posting=posting,
        )

    # ------------------------------------------------------------------ #
    def _react_loop(
        self,
        doc: ExtractedDocument,
        charge: ChargeLine,
        tools: AgentTools,
        settings: Settings,
        doc_issues: list[Issue],
        duplicate_of: str | None,
    ) -> dict[str, Any] | None:
        """ReAct 循环：模型出动作 → 执行工具 → 观察回喂，直到 finish 或超限。"""
        history: list[str] = []
        for round_index in range(1, self.max_rounds + 1):
            prompt = self._build_prompt(doc, charge, settings, doc_issues, duplicate_of, history)
            try:
                text = self.llm.complete(prompt)
            except Exception as exc:  # noqa: BLE001 - 模型故障回落确定性推理
                logger.warning("综合 agent LLM 调用失败，回落确定性推理: %s", exc)
                return None
            action = _parse_action(text)
            if action is None:
                history.append(f"第{round_index}回合：输出无法解析为 JSON，请严格只输出一个 JSON 对象。")
                continue
            if action.get("action") == "finish":
                proposal = action.get("proposal")
                return proposal if isinstance(proposal, dict) else {}
            observation = tools.dispatch(str(action.get("action")), action.get("args"))
            history.append(
                f"第{round_index}回合动作: {json.dumps(action, ensure_ascii=False)}\n"
                f"观察: {json.dumps(observation, ensure_ascii=False)}"
            )
        logger.warning("综合 agent ReAct 循环超过 %d 回合未收敛，回落确定性推理", self.max_rounds)
        return None

    def _build_prompt(
        self,
        doc: ExtractedDocument,
        charge: ChargeLine,
        settings: Settings,
        doc_issues: list[Issue],
        duplicate_of: str | None,
        history: list[str],
    ) -> str:
        """装配工作台上下文：主体 + 当前费用 + 融合草稿 + 校验 + 历史观察。"""
        issues_text = "; ".join(f"{i.code}:{i.message}" for i in doc_issues) or "无"
        warnings_text = "; ".join(doc.warnings[:12]) or "无"
        provenance = charge.provenance.extractor if charge.provenance else "rules"
        context = f"""===== 记账主体 =====
{settings.accounting_entity}

===== 当前费用行 =====
原始名: {charge.raw_name}
草稿标准名: {charge.charge_item}（字典收录={charge.standardized}）
金额: {charge.amount} {charge.currency}  数量: {charge.quantity}  单价: {charge.unit_rate}  红字标记: {charge.amount_sign}
抽取来源: {provenance}

===== 融合草稿（规则+LLM，warnings 标注 LLM 补充与两稿冲突）=====
单据类型: {doc.document_type}  编号: {doc.effective_reference()}  日期: {doc.document_date}
签发方: {doc.issuer}  接收方: {doc.recipient}
总额: {doc.total}  草稿方式: {doc.method}
草稿备注/冲突: {warnings_text}

===== 确定性校验问题 =====
{issues_text}
本次重复登记: {duplicate_of or '无'}

===== 历史动作与观察 =====
{chr(10).join(history) if history else '（无）'}

请输出下一个 JSON 动作。"""
        return f"{SYSTEM_PROMPT}\n\n{context}"

    def _deterministic_proposal(
        self,
        doc: ExtractedDocument,
        charge: ChargeLine,
        tools: AgentTools,
        settings: Settings,
    ) -> Proposal:
        """ReAct 不收敛时的兜底：用确定性四级匹配 + 四要素推理产提案。"""
        match = match_charge(charge, doc, tools.ledger, settings)
        posting = decide_posting(
            entity=settings.accounting_entity,
            issuer=doc.issuer,
            recipient=doc.recipient,
            document_type=doc.document_type,
            amount_sign=charge.amount_sign,
        )
        reasons = ["ReAct 循环未在限期内收敛，回落确定性推理"]
        reasons.extend(match.notes)
        reasons.extend(posting.reasoning)
        return Proposal(
            role=posting.role,
            charge_item=charge.charge_item,
            standardized=charge.standardized,
            reasons=reasons,
            confidence=posting.confidence,
            match=match,
            posting=posting,
        )


class ChatAgentLlm:
    """综合 agent 的真实 LLM 适配器（OpenAI 兼容接口，温度 0）。

    受控面与假实现一致：``.enabled`` / ``.complete(prompt) -> str``。
    懒构造 ChatOpenAI，仅在真正调用时联网；测试注入假实现，不会构造它。
    "必须配 key、无 key 报错"的强约束由票06 接入。
    """

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.enabled = bool(settings.llm_enabled and settings.llm_api_key)
        self._model = None

    def _build(self):
        if self._model is None:
            from langchain_openai import ChatOpenAI

            self._model = ChatOpenAI(
                model=self.settings.llm_model,
                base_url=self.settings.llm_base_url,
                api_key=self.settings.llm_api_key,
                temperature=0.0,
                timeout=self.settings.llm_timeout,
                max_retries=self.settings.llm_max_retries,
            )
        return self._model

    def complete(self, prompt: str) -> str:
        """把整段工作台提示词发给模型，返回文本（一个 JSON 动作）。"""
        response = self._build().invoke(prompt)
        return response.content if hasattr(response, "content") else str(response)


def _annotate_agent(assessment: RecordAssessment, proposal: Proposal) -> None:
    """把 agent 的理由/自标置信度/角色候选写进 audit_trail（票04 验收）。"""
    audit = assessment.record.audit_trail
    for reason in proposal.reasons:
        audit.ai_reasoning.append(f"综合 agent：{reason}")
    audit.agent_confidence = proposal.confidence
    audit.agent_role = proposal.role


def synthesize_charge(
    owner: "BillingAgent", payload: dict[str, Any], *, raw: RawDocument | None
) -> RecordAssessment:
    """第三段综合入口：agent 激活走 ReAct 提案+死规则装配，否则走确定性链。

    Args:
        owner: 编排器实例（提供台账、配置、入账链、agent LLM 注入缝）。
        payload: 单据/费用/单据级校验/重复登记/配置（与票02 同形）。
        raw: 当前文件原件，供 read_raw 工具回原件查证。

    Returns:
        一条费用的完整处理结论 RecordAssessment（提案，尚未落账）。
    """
    agent_llm = getattr(owner, "agent_llm", None)
    if agent_llm is None or not getattr(agent_llm, "enabled", False):
        # 未注入综合 agent LLM：沿用票02 确定性入账链，行为不变。
        return owner.record_chain.invoke(payload)

    settings: Settings = payload["settings"]
    doc: ExtractedDocument = payload["document"]
    charge: ChargeLine = payload["charge"]
    try:
        tools = AgentTools(
            ledger=owner.ledger, settings=settings, raw=raw, doc=doc, charge=charge
        )
        proposal = ComprehensiveAgent(agent_llm).propose(
            doc,
            charge,
            tools=tools,
            settings=settings,
            doc_issues=payload.get("doc_issues") or [],
            duplicate_of=payload.get("duplicate_of"),
        )
        issues = validate_charge(charge, doc)
        issues.extend(payload.get("doc_issues") or [])
        assessment = assemble_record(
            {**payload, "match": proposal.match, "posting": proposal.posting, "issues": issues}
        )
        _annotate_agent(assessment, proposal)
        return assessment
    except Exception:  # noqa: BLE001 - agent 异常不拖垮单条费用，回落确定性链
        logger.exception("综合 agent 装配失败，回落确定性入账链: %s", doc.effective_reference())
        return owner.record_chain.invoke(payload)


__all__ = [
    "ChatAgentLlm",
    "ComprehensiveAgent",
    "MAX_ROUNDS",
    "Proposal",
    "synthesize_charge",
]
