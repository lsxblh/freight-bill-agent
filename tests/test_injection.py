"""主缝注入回归测试（票01）：假 LLM / 假 OCR 端到端。

证明 ``BillingAgent(settings).run(paths)`` 的主缝可以注入假客户端：
* 假 OCR 提供原件文本——不联网、不读真实缓存；
* 假 LLM 提供 LLM 草稿——不构造真实 ChatOpenAI；
* 输出记录的字段只能来自假客户端（canned 文本里没有任何可被规则
  解析的字段），以此证明注入真正生效，而不是走了默认客户端。
"""

from __future__ import annotations

from pathlib import Path

from freight_bill.config import Settings
from freight_bill.extract.llm_extractor import LlmCharge, LlmDocument
from freight_bill.ingest.ocr_client import OcrOutcome
from freight_bill.pipeline import BillingAgent

#: 假 OCR 返回的文本：规则从中抽不出任何字段，单据内容完全由假 LLM 草稿提供。
CANNED_OCR_TEXT = (
    "Canned OCR text for the injection test.\n"
    "No bill fields can be parsed from this text.\n"
)


class FakeOcrClient:
    """假 OCR：记录调用并返回固定文本，接口与 OcrClient 的受控面一致。"""

    def __init__(self, settings: Settings, text: str) -> None:
        self.settings = settings
        self._text = text
        self.calls: list[Path] = []
        self.stats = {"api_calls": 0, "cache_hits": 0, "failures": 0, "replays": 0}

    def process_file(self, path: Path) -> OcrOutcome:
        """记录被调用的文件，返回 canned 识别结果。"""
        self.calls.append(Path(path))
        return OcrOutcome(text=self._text, confidence=0.99, source="api")

    def _load_cache(self, sha: str) -> None:
        """PDF 文本层足够时会查缓存补表格；假客户端一律无缓存。"""
        return


class FakeLlmExtractor:
    """假 LLM：记录调用并返回固定草稿，接口与 LlmExtractor 的受控面一致。"""

    enabled = True

    def __init__(self, draft: LlmDocument) -> None:
        self._draft = draft
        self.calls = 0
        self.stats = {"calls": 0, "cache_hits": 0, "failures": 0, "repairs": 0}

    def extract(self, text: str, *, filename: str, kind: str) -> LlmDocument:
        """记录调用次数，返回预置草稿。"""
        self.calls += 1
        self.stats["calls"] += 1
        return self._draft


def _fake_draft() -> LlmDocument:
    """构造一份完整的 LLM 草稿：规则草稿为空时，合并后字段全部来自它。"""
    return LlmDocument(
        reference_no="FAKE-001",
        document_date="2026-11-01",
        document_type="Debit Note",
        issuer="NEPTUNE SHIPPING LIMITED",
        recipient="ALL AMERICA FORWARDING, INC.",
        currency="USD",
        total_amount=88.88,
        charges=[
            LlmCharge(
                raw_name="Handling Fee",
                amount=88.88,
                currency="USD",
                evidence="HANDLING FEE USD 88.88",
            )
        ],
        confidence=0.9,
    )


def test_main_seam_injects_fake_llm_and_ocr(
    settings: Settings, samples_dir: Path
) -> None:
    """主缝注入假 LLM + 假 OCR：端到端跑通，且输出只能来自假客户端。"""
    fake_ocr = FakeOcrClient(settings, CANNED_OCR_TEXT)
    fake_llm = FakeLlmExtractor(_fake_draft())
    image_path = samples_dir / "图片示例.png"

    agent = BillingAgent(settings, ocr_client=fake_ocr, llm=fake_llm)
    result = agent.run([image_path])

    # 两个假客户端都被真正调用（图片必须走 OCR；LLM 草稿被请求一次）
    assert fake_ocr.calls == [image_path]
    assert fake_llm.calls == 1

    # 端到端跑通：无失败，产出唯一一条记录
    assert not result.failures
    assert len(result.records) == 1
    record = result.records[0]

    # canned 文本抽不出任何字段：以下字段全部来自假 LLM 草稿
    assert record.document_info.reference_no == "FAKE-001"
    assert record.charge_details.charge_item == "Handling Charge"
    assert record.charge_details.amount == 88.88
    assert record.charge_details.currency == "USD"
    assert record.operation == "CREATE"
    assert record.financial_attributes.posting_type == "AR"
    # 费用行的来源标记为 LLM，且带有"LLM 补充"提示：证明草稿来自假 LLM
    assert record.audit_trail.source["extractor"] == "llm"
    assert any("LLM" in warning for warning in record.audit_trail.warnings)
