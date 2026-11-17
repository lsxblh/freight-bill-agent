"""端到端流水线。

    文件 → 解析（EML/PDF/图片/Excel/Word，附件递归）
         → 抽取（规则 ∥ LLM，交叉校验）
         → 标准化（费用名/币种/付款方式）
         → 校验（金额/币种/合计/OCR 混淆/重复）
         → 匹配（4 级规则）
         → 记账方向（AR/AP）
         → 增量写入台账
         → 标准 JSON + 审计链 + 运行报告

票02 起内部编排为四段式 LangGraph StateGraph（读进来 → 双路抽取 →
综合 → 尾巴，见 freight_bill.graph），本模块是图的薄封装：
批次循环、运行日志、汇总与输出。
"""

from __future__ import annotations

import time
import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .agent import build_record_chain
from .config import Settings
from .extract.document_extractor import DocumentExtractor
from .extract.llm_extractor import LlmExtractor
from .graph import build_billing_graph
from .graph.agents import ChatAgentLlm
from .graph.graph import RECURSION_LIMIT
from .graph.judge import LlmJudge
from .graph.repair import RepairAgent
from .harness.report import build_markdown, write_markdown
from .harness.runlog import FileLog, RunLog
from .ingest import OcrClient, collect_input_files
from .io_utils import get_logger, write_json
from .matching.ledger import BillLine, Ledger, norm_key
from .models import ExtractedDocument, OutputRecord
from .pipeline_utils import RecordAssessment
from .validation.validators import Issue, summarize

logger = get_logger("pipeline")


class MissingAgentKeyError(RuntimeError):
    """命脉模式下综合 agent 不可用（无 key 且无注入）时抛出。

    规格 Q5 已批准的行为变更：综合 agent 是命脉，无 API key 必须明确
    报错，不静默降级到确定性链。仅当 settings.agent_required_key=True
    （环境变量 AGENT_REQUIRED_KEY）时触发。
    """


@dataclass
class PipelineResult:
    records: list[OutputRecord] = field(default_factory=list)
    assessments: list[RecordAssessment] = field(default_factory=list)
    documents: list[ExtractedDocument] = field(default_factory=list)
    ledger: Ledger | None = None
    summary: dict[str, Any] = field(default_factory=dict)
    run_log: RunLog | None = None
    failures: list[dict[str, Any]] = field(default_factory=list)
    settings: Settings | None = None

    def to_json_list(self) -> list[dict[str, Any]]:
        return [record.to_json_dict() for record in self.records]

    def operations(self) -> dict[str, int]:
        return self.summary.get("operations", {})


class BillingAgent:
    """智能账单助手（对外唯一入口）。"""

    def __init__(
        self,
        settings: Settings | None = None,
        *,
        ledger: Ledger | None = None,
        ocr_client: OcrClient | None = None,
        llm: LlmExtractor | None = None,
        agent_llm: Any = None,
        judge: LlmJudge | None = None,
        repair: RepairAgent | None = None,
    ) -> None:
        """初始化编排器。

        Args:
            settings: 运行配置；缺省时从环境变量构造。
            ledger: 台账；缺省时按 settings.ledger_path 加载。
            ocr_client: OCR 客户端注入缝；缺省时按配置构造真实客户端。
            llm: LLM 抽取器注入缝；测试/演示可注入假实现，
                缺省时按配置构造真实抽取器。注入优先，编排层不再
                无条件硬造客户端。
            agent_llm: 综合 agent 的 LLM 注入缝（票04）；受控面为
                .enabled / .complete(prompt)->str。缺省 None 时综合段
                沿用确定性入账链；注入启用实现时走 ReAct 工作台。
                票06 命脉模式（settings.agent_required_key=True）下
                必须可用：无 key 且无注入→构造抛 MissingAgentKeyError。
            judge: 软失败裁判注入缝（票03）；缺省时按 JUDGE_MODEL 配置
                构造，未启用则为 None（退化为纯代码指标）。
            repair: 解析补救 agent 注入缝（票03）；缺省时用注入的 OCR
                客户端构造，离线可回放。
        """
        self.settings = settings or Settings.from_env()
        self.ocr = ocr_client or OcrClient(self.settings)
        self.llm = llm or LlmExtractor(self.settings)
        self.agent_llm = self._resolve_agent_llm(agent_llm)
        self.extractor = DocumentExtractor(self.settings, self.llm)
        self.judge = judge if judge is not None else self._build_judge()
        self.repair = repair if repair is not None else RepairAgent(self.ocr, self.settings)
        self.ledger = ledger if ledger is not None else Ledger.load(
            self.settings.ledger_path or "", entity=self.settings.accounting_entity
        )
        self.record_chain = build_record_chain(
            {"ledger": self.ledger, "settings": self.settings}
        )
        self._graph = None
        self._doc_bill_ids: dict[int, str] = {}
        self._duplicates: dict[tuple, str] = {}

    # ------------------------------------------------------------------ #
    def _resolve_agent_llm(self, agent_llm: Any) -> Any:
        """解析综合 agent LLM：命脉模式把关，兼容模式保持票04 行为。

        Args:
            agent_llm: 主缝注入的实现（可为 None）。

        Returns:
            注入的实现原样返回；未注入且兼容模式返回 None（确定性链）；
            未注入但命脉模式且配了 key 时默认启用 ChatAgentLlm。

        Raises:
            MissingAgentKeyError: 命脉模式（agent_required_key=True）下
                未注入且无可用 key——明确报错，不静默降级。
        """
        if agent_llm is not None:
            if self.settings.agent_required_key and not getattr(agent_llm, "enabled", False):
                raise MissingAgentKeyError(
                    "综合 agent 是本项目命脉（AGENT_REQUIRED_KEY=true）：注入的 agent_llm "
                    "未启用（.enabled=False），拒绝静默降级。请注入启用的 agent_llm，"
                    "或配置 LLM_API_KEY 并设 LLM_ENABLED=true；确需回退确定性链时请显式设 "
                    "AGENT_REQUIRED_KEY=false。"
                )
            return agent_llm
        if not self.settings.agent_required_key:
            return None
        if self.settings.llm_enabled and self.settings.llm_api_key:
            # 命脉模式默认启用：有 key 就接真实适配器（懒构造，调用时才联网）。
            return ChatAgentLlm(self.settings)
        raise MissingAgentKeyError(
            "综合 agent 是本项目命脉（AGENT_REQUIRED_KEY=true）：未配置 LLM_API_KEY "
            "或未启用 LLM_ENABLED，且未注入可用的 agent_llm，拒绝静默降级。"
            "请配置 LLM_API_KEY 并设 LLM_ENABLED=true，或经主缝注入 agent_llm；"
            "确需回退确定性链时请显式设 AGENT_REQUIRED_KEY=false。"
        )

    # ------------------------------------------------------------------ #
    def _build_judge(self) -> LlmJudge | None:
        """按配置构造软失败裁判；未启用返回 None（退化为纯代码指标，不阻断离线）。"""
        if self.settings.judge_enabled and self.settings.judge_model:
            return LlmJudge(self.settings)
        return None

    # ------------------------------------------------------------------ #
    def graph(self):
        """四段式 StateGraph（懒编译）：读进来 → 双路抽取 → 综合 → 尾巴。

        票02 起内部编排走图；节点仍为死规则，行为与旧循环一致。
        """
        if self._graph is None:
            self._graph = build_billing_graph(self)
        return self._graph

    # ------------------------------------------------------------------ #
    def run(self, inputs: Sequence[str | Path]) -> PipelineResult:
        run_log = RunLog()
        run_log.start(time.strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:6])
        started = time.perf_counter()

        files = collect_input_files(list(inputs))
        logger.info("待处理文件 %d 个", len(files))

        result = PipelineResult(ledger=self.ledger, settings=self.settings, run_log=run_log)
        self._doc_bill_ids.clear()
        self._duplicates.clear()

        graph = self.graph()
        for path in files:
            entry = FileLog(file=path.name)
            file_started = time.perf_counter()
            # 单文件走四段式图；失败隔离与逐条落账都在图内完成，
            # 这里只收拢结果、计时并登记运行日志。
            final = graph.invoke(
                {"path": path, "entry": entry},
                config={"recursion_limit": RECURSION_LIMIT},
            )
            result.documents.extend(final.get("documents") or [])
            result.records.extend(final.get("records") or [])
            result.assessments.extend(final.get("assessments") or [])
            result.failures.extend(final.get("failures") or [])
            entry.duration_ms = int((time.perf_counter() - file_started) * 1000)
            run_log.add_file(entry)
            run_log.bump("files_failed" if entry.status == "failed" else "files_ok")

        run_log.stop()

        operations: dict[str, int] = {}
        postings = {"AR": 0, "AP": 0, "unknown": 0}
        all_issues: list[Issue] = []
        for assessment in result.assessments:
            operations[assessment.operation] = operations.get(assessment.operation, 0) + 1
            posting = assessment.record.financial_attributes.posting_type
            postings[posting or "unknown"] = postings.get(posting or "unknown", 0) + 1
            all_issues.extend(assessment.issues)

        result.summary = {
            "files": len(files),
            "files_failed": len(result.failures),
            "documents": len(result.documents),
            "records": len(result.records),
            "operations": operations,
            "posting": postings,
            "issues": summarize(all_issues),
            "duration_s": round(time.perf_counter() - started, 3),
            "run_id": run_log.run_id,
            "ledger": self.ledger.stats(),
            "ocr": dict(self.ocr.stats),
            "llm": self.extractor.describe(),
        }
        run_log.counters.update(
            {
                "records": len(result.records),
                "documents": len(result.documents),
                **{f"op_{k}": v for k, v in operations.items()},
            }
        )
        run_log.meta = {
            "entity": self.settings.accounting_entity,
            "ocr_mode": self.settings.ocr_mode,
            "llm_enabled": self.llm.enabled,
        }
        logger.info(
            "处理完成：%d 文件 / %d 单据 / %d 记录，耗时 %.2fs",
            len(files),
            len(result.documents),
            len(result.records),
            result.summary["duration_s"],
        )
        return result

    # ------------------------------------------------------------------ #
    def _lookup_duplicate(self, doc: ExtractedDocument, charge) -> str | None:
        key = self._duplicate_key(doc, charge)
        if key is None:
            return None
        return self._duplicates.get(key)

    def _register_duplicate(self, doc: ExtractedDocument, charge, assessment: RecordAssessment) -> None:
        key = self._duplicate_key(doc, charge)
        if key is None or key in self._duplicates:
            return
        self._duplicates[key] = (
            f"{doc.source.filename if doc.source else '?'}:{charge.charge_item}@{charge.amount}"
        )

    def _duplicate_key(self, doc: ExtractedDocument, charge) -> tuple | None:
        reference = doc.effective_reference()
        if not reference:
            return None
        return (
            norm_key(reference),
            (charge.charge_item or "").strip().lower(),
            round(charge.amount or 0.0, 2),
            (charge.currency or "").upper(),
        )

    def _apply_to_ledger(self, assessment: RecordAssessment, doc: ExtractedDocument, charge) -> None:
        if assessment.operation == "CREATE":
            bill_id = self._bill_id_for(doc)
            existing = [line for line in self.ledger.lines if line.bill_id == bill_id]
            line = BillLine(
                bill_id=bill_id,
                line_id=f"{bill_id}-L{len(existing) + 1}",
                charge_item=charge.charge_item,
                amount=charge.signed_amount,
                currency=charge.currency,
                payment_term=charge.payment_term,
                document_type=doc.document_type,
                reference_no=doc.reference_no,
                document_date=doc.document_date,
                issuer=doc.issuer,
                recipient=doc.recipient,
                mbl_no=doc.identifiers.mbl_no,
                hbl_no=doc.identifiers.hbl_no,
                bl_no=doc.identifiers.bl_no,
                booking_no=doc.identifiers.booking_no,
                vessel=doc.identifiers.vessel,
                containers=list(doc.identifiers.container_nos),
                posting_type=assessment.record.financial_attributes.posting_type,
                accounting_entity=self.settings.accounting_entity,
                source=(doc.source.filename if doc.source else None),
                note="由智能账单助手自动新增",
            )
            self.ledger.add(line)
        elif assessment.operation in {"UPDATE", "NO_CHANGE"} and assessment.bill_line is not None:
            if assessment.updates:
                assessment.bill_line.apply_changes(assessment.updates)
            if not assessment.bill_line.posting_type:
                assessment.bill_line.posting_type = (
                    assessment.record.financial_attributes.posting_type
                )
            assessment.bill_line.source = doc.source.filename if doc.source else assessment.bill_line.source

    def _bill_id_for(self, doc: ExtractedDocument) -> str:
        key = id(doc)
        if key not in self._doc_bill_ids:
            self._doc_bill_ids[key] = self.ledger.next_bill_id()
        return self._doc_bill_ids[key]

    # ------------------------------------------------------------------ #
    def write_outputs(self, result: PipelineResult, out_dir: Path | str | None = None) -> dict[str, Path]:
        target = Path(out_dir or self.settings.output_dir or "output")
        target.mkdir(parents=True, exist_ok=True)

        results_path = write_json(target / "results.json", result.to_json_list())
        ledger_path = write_json(target / "ledger_after.json", self.ledger.model_dump(mode="json"))
        run_log_path = write_json(
            target / "run_log.json",
            result.run_log.to_dict() if result.run_log else {},
        )
        report = build_markdown(
            records=result.records,
            summary=result.summary,
            settings=self.settings,
            run_log=result.run_log.to_dict() if result.run_log else None,
            documents=result.documents,
            deterministic=not self.llm.enabled,
        )
        report_path = write_markdown(target / "report.md", report)

        if self.settings.ledger_path and result.records:
            # 只写回台账副本，避免污染原始台账（需要时用 --write-ledger 显式开启）
            pass
        return {
            "results": results_path,
            "ledger": ledger_path,
            "run_log": run_log_path,
            "report": report_path,
        }


__all__ = ["BillingAgent", "MissingAgentKeyError", "PipelineResult"]
