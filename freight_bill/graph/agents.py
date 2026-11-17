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
- match_charge：认亲（四级匹配）。无参数。返回 matched/bill_id/rule_label/conflicts/notes/matched_line（命中的台账行事实，含金额/币种/双方/日期）。
- read_raw：读原文。参数 keyword（可选）。返回原文片段。
- query_ledger：查台账。参数 reference/charge_item（可选）。返回按记账主体隔离的台账行。
- lookup_charge_name：查费用名字典。参数 raw_name（可选，默认当前费用）。返回 standard/known/confidence。

每一回合只输出一个 JSON 对象，不要输出任何其它文字：
- 调工具：{"action": "<工具名>", "args": {...}}
- 产提案：{"action": "finish", "proposal": {"role": "issuer"|"recipient"|null, "charge_item": "<标准名>", "reasons": ["..."], "confidence": 0.0~1.0}}

===== 你的输出边界（重要）=====
- 你只产角色候选（issuer/recipient/null）、标准名、理由、置信度。
- AR/AP 由死规则从你的角色落出：issuer→基础 AR，recipient→基础 AP；Credit Note/红字行的方向反转由死规则完成。你不输出 AR/AP，不要自己反转方向。
- operation（CREATE/UPDATE/NO_CHANGE/PENDING_REVIEW）完全由尾巴死规则按"认亲事实+字段变化"算出，你不输出 operation，也不要因为拿不准 operation 而降低置信度。
- confidence 表示你对"角色判断与事实"的把握，不是对 operation 的把握。

===== operation 四值语义（供你理解与写理由）=====
- CREATE：四级认亲全部未命中，台账无此费用 → 新增入账。
- UPDATE：认亲命中，且输入材料明确改写了台账已有的非空值（金额、币种、双方、日期等实质变化）→ 增量更新。
- NO_CHANGE：认亲命中且无实质改写——各字段一致，或仅补全台账的空字段。补全空字段不算更新。
- PENDING_REVIEW：重复登记/阻断性校验/我方角色判不出/认亲冲突/低置信 → 转人工，不自动入账。
判定依据：认没认出由 match_charge 工具事实决定；有没有"实质变化"由尾巴逐字段比对台账与单据决定（改写已有值才算，补空值不算）。

===== 角色怎么判（四要素推理）=====
四要素 = 记账主体（我方公司）+ 签发方 + 接收方 + 单据类型/收费方向。按顺序推理：
1. 把记账主体分别与签发方、接收方做公司名模糊比对（大小写/标点/分支机构后缀如 (LAX)/常见缩写差异视为同一家公司）。
2. 主体≈签发方 → role=issuer：我方对外签发单据收费（Debit Note/Invoice 等）。
3. 主体≈接收方 → role=recipient：我方被对方开票/收费。
4. 两者都不匹配 → role=null：双方都是别的公司，或是邮箱地址/人名等无法与公司名比对的对象。绝不单凭单据类型猜角色。
注意：四要素是推理输入，不是必须齐全的清单。接收方缺失但签发方明确是记账主体时，role=issuer 依然成立；不要因字段缺失就置 null。

===== 置信度标定 =====
- 公司名匹配明确、单据类型明确：0.85~0.95。
- 轻微歧义（别名/分支、双方都与主体有些相似）：0.7~0.85，并在理由里说明歧义。
- 确实判不出：role=null，置信度可低；不要为抬高置信度而臆造角色。
- 事实明确时不要低于 0.5：尾巴把自标置信度 <0.5 视为不可信，会整条拦下转人工。

===== 对照示例（工具确认事实后 → finish 提案）=====
例1（UPDATE，金额实质改写）：
事实：match_charge 命中 BILL-1001，台账金额 40.00 USD，单据金额 65.00 USD；Debit Note，签发方=记账主体 NEPTUNE SHIPPING LIMITED。
提案：{"action":"finish","proposal":{"role":"issuer","charge_item":"Handling Charge","reasons":["认亲命中 BILL-1001，金额 40.00→65.00 属实质改写，尾巴将判 UPDATE","签发方即记账主体，我方签发借记单收费，role=issuer，死规则落 AR"],"confidence":0.9}}
例2（NO_CHANGE，金额一致；同名第二条）：
事实：match_charge 命中 BILL-1002，台账金额 35.00 USD = 单据金额 35.00 USD，其余字段一致；Debit Note，签发方=记账主体；同单据同名费用第二条，按金额精确命中。
提案：{"action":"finish","proposal":{"role":"issuer","charge_item":"Handling Charge","reasons":["认亲命中且金额/币种/双方一致，无实质改写，尾巴将判 NO_CHANGE","同名第二条按金额命中，不与第一条的 UPDATE 混淆"],"confidence":0.9}}
例3（NO_CHANGE，补全不算更新）：
事实：认亲命中，台账金额/币种/双方均与单据一致，仅台账编号为空、单据有编号；Arrival Notice，签发方=记账主体。
提案：{"action":"finish","proposal":{"role":"issuer","charge_item":"On Carriage Charge","reasons":["仅补全台账空编号，补全不构成改写，尾巴将判 NO_CHANGE"],"confidence":0.9}}
例4（CREATE，台账无此费用）：
事实：match_charge 四级均未命中（matched=false）；Debit Note，签发方=记账主体。
提案：{"action":"finish","proposal":{"role":"issuer","charge_item":"Detention Charge","reasons":["台账无此费用，尾巴将判 CREATE","签发方即记账主体，我方签发借记单收费，死规则落 AR"],"confidence":0.9}}
例5（Debit Note 方向，接收方缺失仍可判）：
事实：Debit Note，签发方=NEPTUNE SHIPPING LIMITED（=记账主体），接收方缺失。
提案：{"action":"finish","proposal":{"role":"issuer","charge_item":"Handling Charge","reasons":["签发方即记账主体，我方签发借记单收费；接收方缺失不影响角色判断，死规则落 AR"],"confidence":0.88}}
例6（Credit Note 方向，反转是死规则的事）：
事实：Credit Note，签发方=记账主体 NEPTUNE SHIPPING LIMITED，接收方=CIMC WETRANS INTL LOG CO LTD。
提案：{"action":"finish","proposal":{"role":"issuer","charge_item":"Handling Charge","reasons":["我方签发贷记单退款/冲销，角色仍是 issuer；方向 AR→AP 的反转由死规则完成，我只报角色"],"confidence":0.9}}
例7（红字行，反转仍是死规则的事）：
事实：Debit Note，签发方=记账主体，本行红字标记 amount_sign=-1（红字/贷记行）。
提案：{"action":"finish","proposal":{"role":"issuer","charge_item":"Handling Charge","reasons":["签发方即记账主体，role=issuer；红字行的方向反转由死规则完成"],"confidence":0.88}}
例8（双方都是别的公司 → null）：
事实：Invoice，签发方=ZIM AMERICAN INTEGRATED SHIPPING SERVICES CO., LLC，接收方=ALL AMERICA FORWARDING, INC.，记账主体=NEPTUNE SHIPPING LIMITED。
提案：{"action":"finish","proposal":{"role":null,"charge_item":"Wharfage","reasons":["签发方与接收方均非记账主体或其别名，判不出我方角色，按规范不凭单据类型猜 AR/AP，转人工（PENDING_REVIEW）"],"confidence":0.3}}
例9（双方是邮箱/人名 → null）：
事实：Pre-Alert 邮件，签发方=luka@pltplt.com，接收方=li，记账主体=NEPTUNE SHIPPING LIMITED。
提案：{"action":"finish","proposal":{"role":null,"charge_item":"Handling Charge","reasons":["双方为邮箱地址/人名，无法与记账主体做公司名比对，角色判不出，转人工（PENDING_REVIEW）"],"confidence":0.3}}

===== 准则 =====
- 认亲以 match_charge 工具事实为准，你只推理疑难 case，不臆造台账行。建议流程：先 match_charge 拿认亲事实；角色疑难时 read_raw 回原文核对双方；再 finish。
- 费用名字典权威；未收录保留原名并降低置信度，不臆造标准名。
- 每个判断都要带理由与置信度；理由里写清四要素推理过程与认亲事实。
- 判不出我方角色就把 role 置 null（尾巴会转人工），绝不猜。"""


@dataclass
class Proposal:
    """综合 agent 对一条费用的提案（建议 + 理由 + 置信度 + 确定性事实）。

    Attributes:
        role: 我方角色候选 issuer/recipient；None 表示判不出（尾巴转人工）。
        reasons: agent 的推理理由，写入 audit_trail.ai_reasoning。
        confidence: agent 自标置信度，写入 audit_trail.agent_confidence。
        match: 确定性四级匹配结果（与认亲工具同源）。
        posting: 由角色候选经死规则落出的记账方向。
    """

    role: str | None
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
    owner: BillingAgent, payload: dict[str, Any], *, raw: RawDocument | None
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
    except Exception:
        logger.exception("综合 agent 装配失败，回落确定性入账链: %s", doc.effective_reference())
        assessment = owner.record_chain.invoke(payload)
        # 票09：回落留痕（additive，不改六字段）——生产可区分真 agent 与静默回落。
        assessment.record.audit_trail.warnings.append("agent 装配异常，回落确定性链")
        return assessment


__all__ = [
    "MAX_ROUNDS",
    "ChatAgentLlm",
    "ComprehensiveAgent",
    "Proposal",
    "synthesize_charge",
]
