"""票03：软失败闸门 + 解析补救 agent 的端到端验收测试。

测试缝（与规格 Testing Decisions / 票03 派单一致）：
* 主缝 ``BillingAgent(settings, *, ocr_client=..., llm=..., judge=...).run(paths)``：
  注入假 OCR / 假 LLM / 假裁判，只从外部结果（records/failures/summary）断言；
* 图缝 ``BillingAgent.graph()``：观察 gate/repair/pending_review 节点与条件边。

关键设计钉子：
* 闸门是"软失败"筛——读出了文字但不够干净才拦；canned 文本（无账单字段但
  干净）在无裁判时必须放行，否则票01/02 的注入回归会被误伤。
* 补救换路重试 ≤2：第一次读坏 → 重 OCR 救回；始终读坏 → 重试耗尽产出
  PENDING_REVIEW，不拿垃圾跑下游。
* 裁判只吹哨（返回 pass/fail + 理由），不抽取字段。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from freight_bill.config import Settings
from freight_bill.extract.llm_extractor import LlmCharge, LlmDocument
from freight_bill.graph.judge import (
    JudgeDecision,
    code_gate,
    compute_metrics,
    run_gate,
)
from freight_bill.graph.repair import RepairAgent
from freight_bill.ingest.ocr_client import OcrOutcome
from freight_bill.models import RawDocument
from freight_bill.pipeline import BillingAgent

#: 干净的 canned 文本：规则抽不出字段，但"读得出来"。无裁判时闸门必须放行，
#: 字段全部由假 LLM 草稿提供（与票01/02 注入回归同一套路）。
CANNED_OCR_TEXT = (
    "Canned OCR text for the soft-fail gate test.\n"
    "No bill fields can be parsed from this text.\n"
)


class SequenceFakeOcr:
    """假 OCR：按调用次序返回预置结果，模拟"第一次读坏、重 OCR 读好"。

    受控面与 OcrClient 一致（settings/process_file/_load_cache/stats）。
    超出预置数量后重复最后一个结果。
    """

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


class FakeLlmExtractor:
    """假 LLM：返回固定草稿，受控面与 LlmExtractor 一致（enabled/extract/stats）。"""

    enabled = True

    def __init__(self, draft: LlmDocument) -> None:
        self._draft = draft
        self.calls = 0
        self.stats = {"calls": 0, "cache_hits": 0, "failures": 0, "repairs": 0}

    def extract(self, text: str, *, filename: str, kind: str) -> LlmDocument:
        self.calls += 1
        self.stats["calls"] += 1
        return self._draft


class FakeJudge:
    """假裁判：只吹哨（pass/fail + 理由），不抽取。记录被咨询的次数。"""

    def __init__(self, passed: bool, reason: str = "fake judge verdict") -> None:
        self._passed = passed
        self._reason = reason
        self.calls = 0
        self.stats = {"calls": 0}

    def judge(self, raw, metrics) -> JudgeDecision:
        self.calls += 1
        self.stats["calls"] += 1
        return JudgeDecision(passed=self._passed, reason=self._reason)


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


def test_soft_fail_repair_rescues_via_reocr(settings: Settings, samples_dir: Path) -> None:
    """第一次 OCR 读空（软失败）→ 补救重 OCR 读好 → 救回，产出正常记录。

    钉住"文本层空→OCR"的换路重试：假 OCR 第一次返回空、第二次返回干净文本，
    闸门第一次拦下、补救后放行。外部行为：OCR 被调 2 次、无失败、1 条 CREATE。
    """
    ocr = SequenceFakeOcr(
        settings,
        [
            OcrOutcome(text="", confidence=None, source="api"),
            OcrOutcome(text=CANNED_OCR_TEXT, confidence=0.99, source="api"),
        ],
    )
    agent = BillingAgent(settings, ocr_client=ocr, llm=FakeLlmExtractor(_draft()))
    result = agent.run([samples_dir / "图片示例.png"])

    assert len(ocr.calls) == 2, "软失败应触发一次重 OCR 补救"
    assert not result.failures
    assert len(result.records) == 1
    assert result.records[0].operation == "CREATE"
    assert result.records[0].document_info.reference_no == "FAKE-GATE-001"


def test_soft_fail_exhausts_retries_to_pending_review(
    settings: Settings, samples_dir: Path
) -> None:
    """始终读空 → 补救重试到上限（≤2）仍救不回 → PENDING_REVIEW，不跑下游。

    钉住重试计数条件边：假 OCR 恒返回空，闸门每次都拦，补救换路重 OCR 两次后
    耗尽。外部行为：OCR 恰被调 3 次（首读 + 2 次补救）、产 1 条 PENDING_REVIEW、
    原因写进 audit_trail、六字段不虚构；主动转人工不计 failure。
    """
    ocr = SequenceFakeOcr(settings, [OcrOutcome(text="", confidence=None, source="api")])
    agent = BillingAgent(settings, ocr_client=ocr, llm=FakeLlmExtractor(_draft()))
    result = agent.run([samples_dir / "图片示例.png"])

    assert len(ocr.calls) == 3, "首读 + 至多 2 次补救重试，重试计数条件边把关"
    assert not result.failures, "救不回是主动转人工复核，不是批次失败"
    assert len(result.records) == 1
    record = result.records[0]
    assert record.operation == "PENDING_REVIEW"
    assert record.document_info.reference_no is None, "不拿垃圾跑下游、不虚构字段"
    assert record.charge_details.charge_item is None
    assert record.audit_trail.source["extractor"] == "gate"
    assert any("未读到任何文本" in r for r in record.audit_trail.ai_reasoning)
    assert result.summary["operations"].get("PENDING_REVIEW") == 1


def test_judge_passes_boundary_fields_from_llm(settings: Settings, samples_dir: Path) -> None:
    """边界 case（干净但无账单信号）交裁判吹哨；裁判放行后字段仍来自假 LLM。

    钉住"裁判只判定不抽取"：canned 文本无字段，裁判说 pass，最终记录的字段全部
    来自假 LLM 草稿（裁判没有任何抽取产物）。
    """
    ocr = SequenceFakeOcr(
        settings, [OcrOutcome(text=CANNED_OCR_TEXT, confidence=0.99, source="api")]
    )
    judge = FakeJudge(passed=True)
    agent = BillingAgent(
        settings, ocr_client=ocr, llm=FakeLlmExtractor(_draft()), judge=judge
    )
    result = agent.run([samples_dir / "图片示例.png"])

    assert judge.calls == 1, "边界 case 应咨询裁判一次"
    assert not result.failures
    assert len(result.records) == 1
    assert result.records[0].operation == "CREATE"
    assert result.records[0].document_info.reference_no == "FAKE-GATE-001"


def test_judge_fails_boundary_to_pending_review(settings: Settings, samples_dir: Path) -> None:
    """裁判对边界 case 吹哨判负 → 补救重抽仍判负 → 耗尽产 PENDING_REVIEW（含裁判原因）。"""
    ocr = SequenceFakeOcr(
        settings, [OcrOutcome(text=CANNED_OCR_TEXT, confidence=0.99, source="api")]
    )
    judge = FakeJudge(passed=False, reason="文字与账单无关")
    agent = BillingAgent(
        settings, ocr_client=ocr, llm=FakeLlmExtractor(_draft()), judge=judge
    )
    result = agent.run([samples_dir / "图片示例.png"])

    assert judge.calls >= 1
    assert not result.failures
    assert len(result.records) == 1
    record = result.records[0]
    assert record.operation == "PENDING_REVIEW"
    assert any("裁判判定不可靠" in r for r in record.audit_trail.ai_reasoning)


class SelectiveFakeOcr:
    """假 OCR：指定文件名抛异常（模拟硬崩溃），其余返回干净文本。"""

    def __init__(self, settings: Settings, fail_names: set[str], text: str) -> None:
        self.settings = settings
        self._fail = set(fail_names)
        self._text = text
        self.calls: list[Path] = []
        self.stats = {"api_calls": 0, "cache_hits": 0, "failures": 0, "replays": 0}

    def process_file(self, path: Path) -> OcrOutcome:
        self.calls.append(Path(path))
        if Path(path).name in self._fail:
            raise RuntimeError("ocr gateway exploded")
        return OcrOutcome(text=self._text, confidence=0.99, source="api")

    def _load_cache(self, sha: str) -> None:
        return None


def test_hard_failure_isolated_batch_continues(settings: Settings, tmp_path: Path) -> None:
    """单文件硬崩溃隔离、整批继续：坏文件产 PENDING_REVIEW + failure，好文件照常落账。"""
    bad = tmp_path / "bad.png"
    good = tmp_path / "good.png"
    bad.write_bytes(b"not a real image")
    good.write_bytes(b"not a real image either")
    ocr = SelectiveFakeOcr(settings, {"bad.png"}, CANNED_OCR_TEXT)
    agent = BillingAgent(settings, ocr_client=ocr, llm=FakeLlmExtractor(_draft()))
    result = agent.run([bad, good])

    assert len(result.failures) == 1, "硬崩溃计一条 failure"
    assert result.failures[0]["file"] == "bad.png"
    assert result.failures[0]["stage"] == "ingest"
    operations = result.summary["operations"]
    assert operations.get("PENDING_REVIEW") == 1
    assert operations.get("CREATE") == 1, "坏文件崩溃不影响好文件落账（整批继续）"
    by_file = {r.audit_trail.source.get("file"): r for r in result.records}
    assert by_file["bad.png"].operation == "PENDING_REVIEW"
    assert by_file["good.png"].operation == "CREATE"


# --------------------------------------------------------------------------- #
# 闸门 / 补救的确定性单元测试（核心业务逻辑 + 误报回归钉子）
# --------------------------------------------------------------------------- #
def _raw(text: str, *, confidence: float | None = None) -> RawDocument:
    """构造最小原件（仅 doc_id/path/filename 必填），供闸门单元测试。"""
    return RawDocument(
        doc_id="t", path="t.png", filename="t.png", kind="image",
        text=text, ocr_source="api", ocr_confidence=confidence,
    )


def test_code_gate_signals(settings: Settings) -> None:
    """代码指标只在明确垃圾信号时判软失败；干净无信号是边界、干净有信号放行。"""
    assert not code_gate(compute_metrics(_raw("")), settings).passed
    low = code_gate(compute_metrics(_raw("Handling USD 88.88", confidence=0.2)), settings)
    assert not low.passed and "置信度" in low.reason
    corrupt = code_gate(compute_metrics(_raw("Handling USD 1,41O.40", confidence=0.99)), settings)
    assert not corrupt.passed and "金额混入字母" in corrupt.reason
    boundary = code_gate(compute_metrics(_raw(CANNED_OCR_TEXT, confidence=0.99)), settings)
    assert boundary.passed and boundary.boundary
    clean = code_gate(compute_metrics(_raw("Handling Fee USD 88.88", confidence=0.99)), settings)
    assert clean.passed and not clean.boundary


def test_corruption_detection_excludes_identifiers() -> None:
    """回归钉子：箱号/单号/纯数字金额不误报；真 OCR 数字混淆才命中。"""
    identifiers = (
        "MRSU2571577, NO.:GSDRHZMHK331281 NO.:OOLU4055331281 "
        "OOLU4055331281 1,410.40 USD 1,410.40 DC-108478 2026-10-30"
    )
    assert compute_metrics(_raw(identifiers)).corrupt_amounts == []
    confused = compute_metrics(_raw("1,41O.40 5O.OO l,2O0.50")).corrupt_amounts
    assert {"1,41O.40", "5O.OO", "l,2O0.50"} <= set(confused)


def test_run_gate_judge_only_on_boundary(settings: Settings) -> None:
    """裁判只在边界 case 被咨询；非边界不咨询；无裁判边界放行（离线不阻断）。"""
    signal = _raw("Handling USD 88.88", confidence=0.99)
    judge = FakeJudge(passed=False)
    assert run_gate(signal, judge, settings).passed and judge.calls == 0

    boundary = _raw(CANNED_OCR_TEXT, confidence=0.99)
    fail_judge = FakeJudge(passed=False, reason="无关文本")
    verdict = run_gate(boundary, fail_judge, settings)
    assert not verdict.passed and fail_judge.calls == 1 and "裁判判定不可靠" in verdict.reason

    pass_judge = FakeJudge(passed=True)
    assert run_gate(boundary, pass_judge, settings).passed and pass_judge.calls == 1
    assert run_gate(boundary, None, settings).passed


@pytest.mark.parametrize(
    ("text", "confidence", "strategy"),
    [
        ("", None, "ocr"),
        ("Handling USD 1,41O.40", 0.99, "ocr"),
        ("Handling USD 88.88", 0.2, "ocr"),
        (CANNED_OCR_TEXT, 0.99, "reextract"),
    ],
)
def test_repair_plan_strategy(
    settings: Settings, text: str, confidence: float | None, strategy: str
) -> None:
    """补救 agent 按指标自主选策略：空/混淆/低置信→重 OCR，其余→带反馈重抽。"""
    plan = RepairAgent(None, settings).plan(_raw(text, confidence=confidence), "闸门原因")
    assert plan.strategy == strategy
