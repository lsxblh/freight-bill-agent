"""票08：裁判启用路径回归 + 重抽反馈真生效。

测试缝（沿用票03 的主缝/单元缝约定）：
* 裁判缝 ``LlmJudge.judge``：真 LlmJudge + 假 ChatOpenAI（monkeypatch 模型构造），
  钉住 enabled=True 时真建链、真调用——判负必须真正判负，不再因断链
  （_chain 恒 None）抛 AttributeError 被捕获后静默放行；
* 主缝 ``BillingAgent(settings, *, ocr_client=..., llm=..., judge=...).run(paths)``：
  假 OCR / 假 LLM / 假裁判，只从外部结果（records/failures）与假实现受控面
  （OCR 调用次数、LLM 收到的反馈）断言。

关键设计钉子：
* 裁判启用且判负 → 真正判负（修复前恒"故障退化放行"）；
* reextract 重试必须强制重 OCR（换路重读，走 OCR 网关），且 gate_reason
  进入 LLM 草稿反馈，使重试与首次真正不同；
* 重试 ≤2、耗尽 → PENDING_REVIEW 不变；首次放行抽取不带反馈。
"""

from __future__ import annotations

import json
from pathlib import Path

from freight_bill.config import Settings
from freight_bill.extract.llm_extractor import LlmCharge, LlmDocument
from freight_bill.graph.judge import JudgeDecision, LlmJudge, compute_metrics
from freight_bill.ingest.ocr_client import OcrOutcome
from freight_bill.models import RawDocument
from freight_bill.pipeline import BillingAgent

#: 干净的 canned 文本：读得出来但没有任何账单信号（无数字/单号/日期），
#: 代码指标判为边界 case，交裁判吹哨。
CANNED_TEXT = (
    "Canned text layer for the judge and reextract regression test.\n"
    "No bill fields can be parsed from this text at all.\n"
)


def _boundary_raw() -> RawDocument:
    """构造边界 case 原件（干净但无账单信号），供裁判单元测试。"""
    return RawDocument(
        doc_id="t", path="t.png", filename="t.png", kind="image",
        text=CANNED_TEXT, ocr_source="api", ocr_confidence=0.99,
    )


def _judge_settings() -> Settings:
    """裁判启用的最小配置（模型/key 均为假值，真调用被 monkeypatch 拦截）。"""
    return Settings(
        judge_enabled=True, judge_model="fake-judge-model", llm_api_key="test-key"
    )


def _patch_judge_model(monkeypatch, payload=None, error=None):
    """把 ChatOpenAI 换成假模型：返回预置 JSON 判定或抛错。

    只替换网络模型这一层，提示词模板与链装配仍走真实 _build()，
    因此能钉住"裁判真的建链并调用"而非仅解析逻辑。

    Returns:
        捕获模型实际收到的提示值列表（断言链被真调用、上下文真传入）。
    """
    from langchain_core.messages import AIMessage
    from langchain_core.runnables import RunnableLambda

    captured: list = []

    def _factory(**kwargs):
        def _invoke(prompt_value):
            captured.append(prompt_value)
            if error is not None:
                raise error
            return AIMessage(content=json.dumps(payload, ensure_ascii=False))

        return RunnableLambda(_invoke)

    monkeypatch.setattr("langchain_openai.ChatOpenAI", _factory)
    return captured


# --------------------------------------------------------------------------- #
# 任务1：真 LlmJudge 启用路径回归（假 chain 驱动）
# --------------------------------------------------------------------------- #
def test_llm_judge_enabled_fail_verdict_really_blocks(monkeypatch) -> None:
    """启用且假 chain 判负 → 必须真正判负（修复前断链静默放行，此测试先红）。"""
    captured = _patch_judge_model(
        monkeypatch, payload={"passed": False, "reason": "文字与账单无关"}
    )
    judge = LlmJudge(_judge_settings())
    assert judge.enabled
    raw = _boundary_raw()

    decision = judge.judge(raw, compute_metrics(raw))

    assert decision.passed is False, "启用时判负必须真正判负，不得静默放行"
    assert "文字与账单无关" in decision.reason
    assert len(captured) == 1, "裁判链应被真实调用一次"
    assert CANNED_TEXT.strip() in captured[0].to_string(), "提示词应携带原件文本上下文"


def test_llm_judge_enabled_pass_verdict_passes(monkeypatch) -> None:
    """启用且假 chain 判正 → 放行，且理由来自 chain 而非退化放行文案。"""
    _patch_judge_model(monkeypatch, payload={"passed": True, "reason": "文本干净"})
    judge = LlmJudge(_judge_settings())
    raw = _boundary_raw()

    decision = judge.judge(raw, compute_metrics(raw))

    assert decision.passed is True
    assert decision.reason == "文本干净", "应采纳 chain 判定，而非故障退化放行的理由"


def test_llm_judge_chain_error_degrades_to_pass(monkeypatch) -> None:
    """假 chain 抛错 → 裁判故障退化为放行（裁判是建议性的，不阻断主链路）。"""
    _patch_judge_model(monkeypatch, error=RuntimeError("judge endpoint exploded"))
    judge = LlmJudge(_judge_settings())
    raw = _boundary_raw()

    decision = judge.judge(raw, compute_metrics(raw))

    assert decision.passed is True
    assert "裁判故障" in decision.reason


def test_llm_judge_disabled_passes_without_chain() -> None:
    """未启用（默认 judge_enabled=False）→ 直接放行，不建链不花 key。"""
    judge = LlmJudge(Settings(judge_enabled=False))
    assert not judge.enabled
    raw = _boundary_raw()

    decision = judge.judge(raw, compute_metrics(raw))

    assert decision.passed is True


# --------------------------------------------------------------------------- #
# 任务2：reextract 换路重读 + gate_reason 进抽取反馈（主缝，假 OCR/假 LLM/假裁判）
# --------------------------------------------------------------------------- #
def _build_canned_pdf(path: Path, lines: list[str]) -> None:
    """手写最小 PDF（纯文本层，无账单信号）。

    文本层字符数 ≥ pdf_text_layer_min_chars，首读不触发 OCR；
    补救重试若带 force_ocr 则必然调用 OCR——据此观察"换路重读"。
    """
    content_parts = ["BT", "/F1 12 Tf", "72 720 Td"]
    for i, line in enumerate(lines):
        if i:
            content_parts.append("0 -16 Td")
        escaped = line.replace("\\", r"\\").replace("(", r"\(").replace(")", r"\)")
        content_parts.append(f"({escaped}) Tj")
    content_parts.append("ET")
    stream = "\n".join(content_parts).encode("latin-1")
    objs = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
        b"/Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n" + stream + b"\nendstream",
    ]
    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for i, body in enumerate(objs, start=1):
        offsets.append(len(out))
        out += f"{i} 0 obj\n".encode() + body + b"\nendobj\n"
    xref_pos = len(out)
    out += f"xref\n0 {len(objs) + 1}\n".encode()
    out += b"0000000000 65535 f \n"
    for off in offsets:
        out += f"{off:010d} 00000 n \n".encode()
    out += (
        f"trailer\n<< /Size {len(objs) + 1} /Root 1 0 R >>\n"
        f"startxref\n{xref_pos}\n%%EOF\n"
    ).encode()
    path.write_bytes(bytes(out))


def _canned_pdf(tmp_path: Path) -> Path:
    """边界 case PDF：文本层干净但无账单信号（无数字/单号/日期）。"""
    pdf = tmp_path / "canned.pdf"
    _build_canned_pdf(
        pdf,
        [
            "Canned text layer for the reextract feedback regression test.",
            "This document contains no bill fields and no amounts at all.",
            "It is only used to exercise the soft fail gate boundary path.",
        ],
    )
    return pdf


class SequenceFakeOcr:
    """假 OCR：按调用次序返回预置结果，超出后重复最后一个。记录调用。"""

    def __init__(self, settings: Settings, outcomes: list[OcrOutcome]) -> None:
        self.settings = settings
        self._outcomes = list(outcomes)
        self.calls: list[Path] = []
        self.stats = {"api_calls": 0, "cache_hits": 0, "failures": 0, "replays": 0}

    def process_file(self, path: Path) -> OcrOutcome:
        self.calls.append(Path(path))
        index = min(len(self.calls) - 1, len(self._outcomes) - 1)
        return self._outcomes[index]

    def _load_cache(self, sha: str) -> None:
        return None


class SequenceFakeJudge:
    """假裁判：前 fail_times 次判负，之后判正。记录咨询次数。"""

    def __init__(self, fail_times: int, reason: str = "文字与账单无关") -> None:
        self._fail_times = fail_times
        self._reason = reason
        self.calls = 0

    def judge(self, raw, metrics):
        self.calls += 1
        if self.calls <= self._fail_times:
            return JudgeDecision(passed=False, reason=self._reason)
        return JudgeDecision(passed=True, reason="重抽后文本可接受")


class FeedbackFakeLlm:
    """假 LLM：返回固定草稿，记录每次调用收到的反馈（受控面含 feedback）。"""

    enabled = True

    def __init__(self, draft: LlmDocument) -> None:
        self._draft = draft
        self.feedbacks: list[str | None] = []
        self.stats = {"calls": 0, "cache_hits": 0, "failures": 0, "repairs": 0}

    def extract(self, text: str, *, filename: str, kind: str, feedback=None) -> LlmDocument:
        self.feedbacks.append(feedback)
        self.stats["calls"] += 1
        return self._draft


def _draft() -> LlmDocument:
    """规则草稿为空时的完整 LLM 草稿（字段全部来自它）。"""
    return LlmDocument(
        reference_no="FAKE-GATE-001",
        document_date="2026-11-01",
        document_type="Debit Note",
        issuer="NEPTUNE SHIPPING LIMITED",
        recipient="ALL AMERICA FORWARDING, INC.",
        currency="USD",
        total_amount=88.88,
        charges=[
            LlmCharge(
                raw_name="Handling Fee", amount=88.88, currency="USD",
                evidence="HANDLING FEE USD 88.88",
            )
        ],
        confidence=0.9,
    )


def test_reextract_retry_forces_ocr_and_feeds_gate_reason(
    settings: Settings, tmp_path: Path
) -> None:
    """裁判首判负 → reextract 重试必须强制重 OCR，且 gate_reason 进 LLM 草稿反馈。

    钉住"重试与首次真正不同"：首读走文本层（OCR 0 次），重试换路走 OCR
    网关（OCR 1 次）；闸门失败原因作为反馈传入抽取，而不是只进运行日志。
    裁判次判正 → 放行抽取，字段来自假 LLM 草稿。
    """
    pdf = _canned_pdf(tmp_path)
    ocr = SequenceFakeOcr(
        settings, [OcrOutcome(text=CANNED_TEXT, confidence=0.99, source="api")]
    )
    judge = SequenceFakeJudge(fail_times=1)
    llm = FeedbackFakeLlm(_draft())
    agent = BillingAgent(settings, ocr_client=ocr, llm=llm, judge=judge)

    result = agent.run([pdf])

    assert len(ocr.calls) == 1, "首读走文本层不 OCR；reextract 重试必须强制重 OCR"
    assert judge.calls == 2, "首判负 + 重试后复判"
    assert llm.feedbacks == ["裁判判定不可靠: 文字与账单无关"], (
        "gate_reason 必须作为反馈进入抽取，而非只进运行日志"
    )
    assert not result.failures
    assert len(result.records) == 1
    assert result.records[0].operation == "CREATE"
    assert result.records[0].document_info.reference_no == "FAKE-GATE-001"


def test_reextract_exhausts_retries_to_pending_review(
    settings: Settings, tmp_path: Path
) -> None:
    """裁判始终判负 → reextract 重试到上限（≤2）仍救不回 → PENDING_REVIEW。

    钉住新路径下重试计数不变：首读文本层 + 2 次强制重 OCR（共 2 次 OCR 调用），
    耗尽产 PENDING_REVIEW（含裁判原因），不跑下游抽取、不计批次失败。
    """
    pdf = _canned_pdf(tmp_path)
    ocr = SequenceFakeOcr(
        settings, [OcrOutcome(text=CANNED_TEXT, confidence=0.99, source="api")]
    )
    judge = SequenceFakeJudge(fail_times=99)
    llm = FeedbackFakeLlm(_draft())
    agent = BillingAgent(settings, ocr_client=ocr, llm=llm, judge=judge)

    result = agent.run([pdf])

    assert len(ocr.calls) == 2, "首读文本层 + 至多 2 次强制重 OCR 重试"
    assert judge.calls == 3, "首判 + 2 次重试后复判"
    assert llm.feedbacks == [], "救不回不跑下游抽取"
    assert not result.failures, "救不回是主动转人工复核，不是批次失败"
    assert len(result.records) == 1
    record = result.records[0]
    assert record.operation == "PENDING_REVIEW"
    assert record.document_info.reference_no is None, "不拿垃圾跑下游、不虚构字段"
    assert any("裁判判定不可靠" in r for r in record.audit_trail.ai_reasoning)
    assert result.summary["operations"].get("PENDING_REVIEW") == 1


def test_first_pass_extraction_carries_no_feedback(
    settings: Settings, tmp_path: Path
) -> None:
    """首次即放行（裁判判正）→ 抽取不带反馈，也不触发 OCR（无重试）。"""
    pdf = _canned_pdf(tmp_path)
    ocr = SequenceFakeOcr(
        settings, [OcrOutcome(text=CANNED_TEXT, confidence=0.99, source="api")]
    )
    judge = SequenceFakeJudge(fail_times=0)
    llm = FeedbackFakeLlm(_draft())
    agent = BillingAgent(settings, ocr_client=ocr, llm=llm, judge=judge)

    result = agent.run([pdf])

    assert len(ocr.calls) == 0, "文本层足够且无重试，不应 OCR"
    assert judge.calls == 1
    assert llm.feedbacks == [None], "首次放行抽取不带闸门反馈"
    assert not result.failures
    assert result.records[0].operation == "CREATE"
