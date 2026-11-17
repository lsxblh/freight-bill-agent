"""软失败闸门：代码指标先筛 + 独立小模型裁判（只吹哨，不抽取）。

职责边界（对应规格 / GLOSSARY"软失败"）：
* 闸门判断的是"读进来的文字够不够干净、能不能可靠抽字段"，不是"有没有抽到
  字段"。字段抽取是双路抽取（规则 ∥ LLM）的事——规则草稿为空、LLM 草稿补全
  是合法路径，所以"干净但没有账单字段"的文本（如注入测试的 canned 文本）在
  无裁判时必须放行，否则会误伤票01/02 的注入回归。
* 代码指标只在出现**明确的垃圾信号**时才判软失败：读不出任何文字、OCR 置信度
  过低、金额里混进字母（OCR 数字混淆）。
* "读得出来但一点账单信号都没有"属于边界 case：有裁判交裁判吹哨，无裁判退化
  为纯代码指标（放行），不阻断离线测试。

裁判（JUDGE_MODEL）只返回 pass/fail + 理由，绝不抽取字段；可注入假裁判离线验证。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

from ..config import Settings
from ..io_utils import get_logger, truncate
from ..models import RawDocument

logger = get_logger("gate")

# 金额数字：千分位或小数（用于"有无金额"信号，非精确解析）。
_AMOUNT_RE = re.compile(r"\d{1,3}(?:,\d{3})*(?:\.\d{1,2})|\d+\.\d{1,2}")
# 单号/箱号：字母-数字 或 字母+数字长串（边界启发式，非精确匹配）。
_REFERENCE_RE = re.compile(r"\b[A-Z]{2,}-\d{2,}\b|\b[A-Z]{4}\d{6,}\b|\b[A-Z]{2,}\d{4,}\b")
# 日期：ISO / 斜杠 / "1 Jan 2026"（仅供裁判上下文，不参与判定）。
_DATE_RE = re.compile(
    r"\b\d{4}-\d{2}-\d{2}\b|\b\d{1,2}/\d{1,2}/\d{2,4}\b|\b\d{1,2}\s+[A-Z][a-z]{2,}\s+\d{4}\b"
)
_LETTER_RE = re.compile(r"[A-Za-z\u4e00-\u9fff]")

# OCR 数字易混字母：0→O/o、1→l/I、5→S/s、8→B/b、2→Z/z。
_CONFUSABLE_LETTERS = frozenset("OoIlSsBbZz")
# 金额 token 的合法字符集：数字 + 小数点/千分位 + 易混字母。
_AMOUNT_CHARS = frozenset("0123456789.,") | _CONFUSABLE_LETTERS


def _corrupt_amount_tokens(text: str) -> list[str]:
    """挑出"金额里混进字母"的 token（OCR 把 0→O、1→l 等数字混淆）。

    先剥掉前缀/后缀的三位币种代码（USD/EUR/CNY…）与货币符号；剩下的 token
    必须**整体**只由数字/小数点/千分位/易混字母组成、且至少含一个易混字母，
    才判为混淆。这样集装箱号/单号（含 M/R/U/N/G/H/K 等非易混字母，如
    MRSU2571577、NO.:GSDRHZMHK331281）天然被排除，纯数字金额也不误报。

    Args:
        text: 原件文本。

    Returns:
        疑似混淆的原始 token 列表（可能为空）。
    """
    bad: list[str] = []
    for token in text.split():
        core = token.strip("$€£¥()")
        core = re.sub(r"^[A-Z]{3}(?=[\d.,])", "", core)
        core = re.sub(r"(?<=[\d.,])[A-Z]{3}$", "", core)
        if not re.search(r"\d", core):
            continue
        if "." not in core and "," not in core:
            continue
        if not all(ch in _AMOUNT_CHARS for ch in core):
            continue
        if not any(ch in _CONFUSABLE_LETTERS for ch in core):
            continue
        bad.append(token)
    return bad


@dataclass
class GateMetrics:
    """闸门代码指标：对原件文本做一次确定性体检。

    Attributes:
        text_chars: 去空白后的文本字符数。
        has_amount: 是否出现金额数字。
        has_charge_line: 是否有"名称+金额"同现的费用行。
        has_reference: 是否出现单号/箱号。
        has_date: 是否出现日期。
        has_tables: 是否有非空表格（Excel/Word 的结构化账单）。
        ocr_confidence: OCR 置信度（文本层/无 OCR 时为 None）。
        corrupt_amounts: 金额混字母的 token 列表。
    """

    text_chars: int
    has_amount: bool
    has_charge_line: bool
    has_reference: bool
    has_date: bool
    has_tables: bool
    ocr_confidence: float | None
    corrupt_amounts: list[str] = field(default_factory=list)


def compute_metrics(raw: RawDocument) -> GateMetrics:
    """对原件计算代码指标（纯函数，同输入同结果）。

    Args:
        raw: 读进来的原件。

    Returns:
        闸门代码指标。
    """
    text = raw.text or ""
    has_charge_line = any(
        _AMOUNT_RE.search(line) and _LETTER_RE.search(line)
        for line in text.splitlines()
    )
    return GateMetrics(
        text_chars=len(text.strip()),
        has_amount=bool(_AMOUNT_RE.search(text)),
        has_charge_line=has_charge_line,
        has_reference=bool(_REFERENCE_RE.search(text)),
        has_date=bool(_DATE_RE.search(text)),
        has_tables=any(not table.is_empty() for table in raw.tables),
        ocr_confidence=raw.ocr_confidence,
        corrupt_amounts=_corrupt_amount_tokens(text),
    )


@dataclass
class GateVerdict:
    """闸门结论。

    Attributes:
        passed: 是否放行到双路抽取。
        reason: 不放行时的原因（供补救反馈与 PENDING_REVIEW）。
        boundary: 是否边界 case（干净但无账单信号，需裁判吹哨）。
    """

    passed: bool
    reason: str = ""
    boundary: bool = False


def code_gate(metrics: GateMetrics, settings: Settings) -> GateVerdict:
    """纯代码指标判定：只在明确垃圾信号时判软失败。

    Args:
        metrics: 代码指标。
        settings: 运行配置（取 gate_min_confidence 阈值）。

    Returns:
        闸门结论；"干净但无账单信号"标记为 boundary 交裁判。
    """
    if metrics.text_chars == 0 and not metrics.has_tables:
        return GateVerdict(False, "未读到任何文本（文本层为空且无表格）")
    if (
        metrics.ocr_confidence is not None
        and metrics.ocr_confidence < settings.gate_min_confidence
    ):
        return GateVerdict(
            False, f"OCR 置信度过低（{metrics.ocr_confidence:.2f}）"
        )
    if metrics.corrupt_amounts:
        return GateVerdict(
            False,
            "金额混入字母（OCR 数字混淆）: " + ", ".join(metrics.corrupt_amounts[:3]),
        )
    # 读得出来但没有任何账单信号 → 边界 case（有裁判交裁判，无裁判放行）。
    if not (
        metrics.has_amount
        or metrics.has_charge_line
        or metrics.has_reference
        or metrics.has_tables
    ):
        return GateVerdict(True, "", boundary=True)
    return GateVerdict(True)


@dataclass
class JudgeDecision:
    """裁判结论：只吹哨（pass/fail + 理由），不抽取任何字段。"""

    passed: bool
    reason: str = ""


class LlmJudge:
    """独立小模型裁判（JUDGE_MODEL）：复用 LLM 端点/key，模型独立配置。

    只问"这段文字够不够干净、能否可靠抽账单字段"，返回 pass/fail + 理由；
    不做字段抽取。裁判自身故障时退化为放行（裁判是建议性的，不阻断主链路）。
    未启用（无 JUDGE_MODEL / 无 key）时 judge() 直接放行。
    """

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.enabled = bool(
            settings.judge_enabled and settings.judge_model and settings.llm_api_key
        )
        self._chain = None
        self.stats = {"calls": 0, "failures": 0}

    def _build(self):
        if self._chain is not None:
            return self._chain
        from langchain_core.prompts import ChatPromptTemplate
        from langchain_openai import ChatOpenAI

        prompt = ChatPromptTemplate.from_messages(
            [
                (
                    "system",
                    "你是账单解析质检裁判。只判断给定文本是否足够干净、能否可靠抽取"
                    "账单字段，不要抽取任何字段。严格输出一个 JSON 对象："
                    '{{"passed": true/false, "reason": "简短理由"}}。',
                ),
                (
                    "human",
                    "代码指标: {metrics}\n\n===== 文本 =====\n{text}\n\n"
                    '请输出 JSON 判定（passed/reason）。',
                ),
            ]
        )
        model = ChatOpenAI(
            model=self.settings.judge_model,
            base_url=self.settings.llm_base_url,
            api_key=self.settings.llm_api_key,
            temperature=0.0,
            timeout=self.settings.llm_timeout,
            max_retries=self.settings.llm_max_retries,
        )
        self._chain = prompt | model
        return self._chain

    def judge(self, raw: RawDocument, metrics: GateMetrics) -> JudgeDecision:
        """对边界 case 吹哨。故障/未启用时放行（不阻断）。

        Args:
            raw: 原件。
            metrics: 代码指标（作为裁判上下文）。

        Returns:
            裁判结论。
        """
        if not self.enabled:
            return JudgeDecision(True, "裁判未启用")
        try:
            self.stats["calls"] += 1
            response = self._chain.invoke(
                {
                    "metrics": json.dumps(metrics.__dict__, ensure_ascii=False),
                    "text": truncate(raw.text or "", 6000),
                }
            )
            content = getattr(response, "content", str(response))
            payload = json.loads(content)
            return JudgeDecision(
                passed=bool(payload.get("passed", True)),
                reason=str(payload.get("reason", "")),
            )
        except Exception as exc:  # noqa: BLE001 - 裁判故障退化为放行
            self.stats["failures"] += 1
            logger.warning("裁判调用失败，退化为放行: %s", exc)
            return JudgeDecision(True, f"裁判故障退化放行: {exc}")


def run_gate(
    raw: RawDocument, judge: LlmJudge | None, settings: Settings
) -> GateVerdict:
    """混合闸门：代码指标先筛，边界 case 交裁判吹哨。

    Args:
        raw: 读进来的原件。
        judge: 裁判实例；None 表示无裁判（退化为纯代码指标）。
        settings: 运行配置。

    Returns:
        最终闸门结论。
    """
    metrics = compute_metrics(raw)
    verdict = code_gate(metrics, settings)
    if not verdict.passed:
        return verdict
    if verdict.boundary and judge is not None:
        decision = judge.judge(raw, metrics)
        if not decision.passed:
            return GateVerdict(False, f"裁判判定不可靠: {decision.reason}")
    return GateVerdict(True)


__all__ = [
    "GateMetrics",
    "GateVerdict",
    "JudgeDecision",
    "LlmJudge",
    "code_gate",
    "compute_metrics",
    "run_gate",
]
