"""四段式图的状态：单个文件处理过程中的三层模型流转。

RawDocument（原件）→ ExtractedDocument/ChargeLine（草稿）→
OutputRecord（记录）。状态只在单文件的图内流转；批次循环、运行日志
与汇总仍由编排层（pipeline）持有。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, TypedDict

from ..harness.runlog import FileLog
from ..models import ChargeLine, ExtractedDocument, OutputRecord, RawDocument
from ..pipeline_utils import RecordAssessment
from ..validation.validators import Issue


class BillingState(TypedDict, total=False):
    """单文件四段式处理的共享状态。

    Attributes:
        path: 当前文件路径。
        entry: 当前文件的运行日志条目（节点原地更新）。
        raw: 读进来的原件；读失败时为 None。
        documents: 双路抽取产出的单据草稿（主单据 + 可独立成账的附件）。
        queue: 待处理费用队列 (单据, 费用行)，综合⇄尾巴逐条消费。
        doc_issues: 单据级校验结果缓存，键为 id(doc)，每单只校验一次。
        assessment: 综合段对当前费用产出的提案；装配失败时为 None。
        charge_error: 当前费用装配失败的错误描述。
        records: 已落账的输出记录。
        assessments: 已落账记录的完整处理结论。
        failures: 本文件的失败条目（ingest/extract/assemble/gate 四级）。
        gate_passed: 软失败闸门是否放行（gate 节点写入）。
        gate_reason: 闸门最近一次不放行的原因（补救反馈 / PENDING_REVIEW）。
        retries: 解析补救已换路重试的次数（0=未补救；条件边据此把关 ≤2）。
        repair_strategy: 本轮补救策略（ocr/reextract），回到 ingest 时消费。
    """

    path: Path
    entry: FileLog
    raw: RawDocument | None
    documents: list[ExtractedDocument]
    queue: list[tuple[ExtractedDocument, ChargeLine]]
    doc_issues: dict[int, list[Issue]]
    assessment: RecordAssessment | None
    charge_error: str | None
    records: list[OutputRecord]
    assessments: list[RecordAssessment]
    failures: list[dict[str, Any]]
    gate_passed: bool
    gate_reason: str | None
    retries: int
    repair_strategy: str | None


__all__ = ["BillingState"]