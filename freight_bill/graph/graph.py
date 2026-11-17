"""四段式 StateGraph 装配：读进来 → 双路抽取 → 综合 → 尾巴。

票02"搬骨架不改行为"：四段全部仍是死规则。综合段暂用现有确定性逻辑
占位（单据校验 + 重复 lookup + 认亲 → 记账方向 → 校验 → 装配），
票04 换成综合 agent（ReAct 工作台）；尾巴负责把关落账，票05 在落账前
加钱关键项确定性交叉验证。

条件边：
* ingest 之后：硬失败隔离（产出 PENDING_REVIEW）；成功则进软失败闸门 gate。
  gate 之后：放行→extract；软失败且重试<上限→repair（换策略回到 ingest 重走）；
  重试耗尽→pending_review（票03）；
* extract 之后：失败隔离 / 空队列收尾；
* tail → synthesize：逐条费用循环。必须逐条"装配→落账→登记重复"，
  因为认亲读的是活台账（先落账的 CREATE 行可能被同单据后一条费用
  认亲），重复登记也按条生效——整单批量装配再落账会改变行为。

依赖方向：pipeline → graph → extract/matching/validation。graph 不
反向 import pipeline，编排器实例由调用方传入（owner）。
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from langgraph.graph import END, START, StateGraph

from ..harness.resilience import retry
from ..ingest import load_document
from ..io_utils import get_logger
from ..validation.validators import validate_document
from .agents import synthesize_charge
from .crosscheck import crosscheck_assessment
from .judge import run_gate
from .repair import build_pending_review_record
from .state import BillingState

if TYPE_CHECKING:
    from ..pipeline import BillingAgent

logger = get_logger("graph")

#: 逐条循环每条费用消耗 2 个超步；放宽递归上限，避免多费用单据
#: 触发 LangGraph 默认 25 超步上限（旧循环没有该限制）。
RECURSION_LIMIT = 10000


def build_billing_graph(owner: BillingAgent):
    """装配并编译四段式骨架图。

    Args:
        owner: 编排器实例。节点经闭包复用其抽取器、明细入账链、台账
            与落账/重复登记助手，保证与旧循环同一套依赖、同一套行为。

    Returns:
        编译后的 StateGraph：输入 {"path": 文件路径, "entry": FileLog}，
        终态含 documents/records/assessments/failures，由编排层收拢。
    """
    settings = owner.settings
    load_with_retry = retry(times=2, base_delay=0.4, logger=logger)(
        lambda path, force_ocr=False: load_document(
            path, ocr_client=owner.ocr, work_dir=settings.work_dir, force_ocr=force_ocr
        )
    )

    def _pending_review_updates(
        state: BillingState, reason: str, *, stage: str, hard: bool = False
    ) -> dict[str, Any]:
        """"放弃→PENDING_REVIEW"的输出更新（硬失败与重试耗尽共用）。

        追加一条 PENDING_REVIEW 记录 + 结论，不跑下游抽取/落账。是否计入
        failure 由 hard 区分——这是两类不同性质的事件：

        * hard=True（ingest 抛异常=硬崩溃）：记一条 failure、日志标记 failed。
          崩溃是意外错误，沿用 failures 机制隔离并上报。
        * hard=False（闸门重试耗尽=主动转人工）：只产 PENDING_REVIEW 记录，
          不计 failure、不标记 failed。PENDING_REVIEW 是复核结论而非批次失败，
          与"记账方向不明转人工"同类（现有端到端里此类记录本就不进 failures）。
          text 模式图片无 OCR 可读即属此类，test_ocr_modes_offline 钉住
          `not result.failures`，故救不回不能误记为失败。

        Args:
            state: 当前单文件状态。
            reason: 转人工的原因。
            stage: failure 条目所属阶段（仅 hard=True 时使用）。
            hard: 是否硬崩溃（计 failure）；默认 False=主动转人工复核。

        Returns:
            records/assessments（hard 时另含 failures）的更新字典。
        """
        path = state["path"]
        entry = state["entry"]
        record, assessment = build_pending_review_record(
            path, state.get("raw"), reason, settings
        )
        records = list(state.get("records") or [])
        assessments = list(state.get("assessments") or [])
        records.append(record)
        assessments.append(assessment)
        entry.records += 1
        entry.operations["PENDING_REVIEW"] = entry.operations.get("PENDING_REVIEW", 0) + 1
        updates: dict[str, Any] = {"records": records, "assessments": assessments}
        if hard:
            failures = list(state.get("failures") or [])
            if not any(f.get("file") == path.name for f in failures):
                failures.append({"file": path.name, "stage": stage, "error": reason})
            entry.status = "failed"
            entry.error = reason
            updates["failures"] = failures
        else:
            entry.warnings.append(f"转人工复核: {reason}")
        return updates

    # ------------------------------------------------------------------ #
    # 四段节点
    # ------------------------------------------------------------------ #
    def ingest(state: BillingState) -> dict[str, Any]:
        """段1 读进来：解析文件（EML/PDF/图片/Excel/Word，附件递归，带重试）。

        补救循环回来时消费 repair_strategy：ocr 与 reextract 都强制重 OCR
        （换路重读，走 OCR 网关），保证重试与首次读取真正不同（票08）。
        硬失败（完全读不出）→ 产出一条 PENDING_REVIEW 记录，不拿垃圾跑下游。
        """
        path = state["path"]
        entry = state["entry"]
        force_ocr = state.get("repair_strategy") in ("ocr", "reextract")
        try:
            raw = load_with_retry(path, force_ocr=force_ocr)
        except Exception as exc:
            entry.status = "failed"
            entry.error = f"{type(exc).__name__}: {exc}"
            logger.exception("解析失败: %s", path)
            return {
                "raw": None,
                **_pending_review_updates(state, entry.error, stage="ingest", hard=True),
            }
        entry.kind = raw.kind
        entry.ocr_source = raw.ocr_source
        # 保留补救重试备注（跨重读不丢反馈），再叠加本次原件警告。
        repair_notes = [w for w in entry.warnings if w.startswith("补救重试")]
        entry.warnings = repair_notes + list(raw.warnings)
        return {"raw": raw}

    def gate(state: BillingState) -> dict[str, Any]:
        """软失败闸门：代码指标先筛，边界 case 交裁判吹哨（票03）。"""
        verdict = run_gate(state["raw"], owner.judge, settings)
        if not verdict.passed:
            logger.info("软失败闸门 %s: %s", state["path"].name, verdict.reason)
        return {"gate_passed": verdict.passed, "gate_reason": verdict.reason or None}

    def repair(state: BillingState) -> dict[str, Any]:
        """解析补救：自主换策略并计数，回到 ingest 重走（票03，重试≤上限）。

        reextract 策略把闸门失败原因留存为 repair_feedback（票08）：gate 放行
        时会清空 gate_reason，故在此单独携带，供 extract 喂进 LLM 草稿提示。
        """
        plan = owner.repair.plan(state["raw"], state.get("gate_reason") or "")
        retries = state.get("retries", 0) + 1
        entry = state["entry"]
        entry.warnings.append(
            f"补救重试 {retries}/{settings.ingest_max_retries}: {plan.reason}"
        )
        logger.info(
            "补救 %s: 策略=%s 重试=%d", state["path"].name, plan.strategy, retries
        )
        feedback = state.get("gate_reason") if plan.strategy == "reextract" else None
        return {
            "retries": retries,
            "repair_strategy": plan.strategy,
            "repair_feedback": feedback,
        }

    def pending_review(state: BillingState) -> dict[str, Any]:
        """重试耗尽：产出 PENDING_REVIEW（含原因），不拿垃圾跑下游（票03）。"""
        reason = state.get("gate_reason") or "重试耗尽仍无法可靠解析"
        return _pending_review_updates(state, reason, stage="gate")

    def extract(state: BillingState) -> dict[str, Any]:
        """段2 双路抽取：规则草稿 ∥ LLM 草稿 → 融合 → 标准化。

        双路链与附件递归仍在 DocumentExtractor 内（build_document_chain），
        本节点只负责把产出的单据排成逐条费用队列。
        """
        path = state["path"]
        entry = state["entry"]
        try:
            # 票08：补救重试携带的闸门原因作为抽取反馈（LLM 草稿提示），
            # 首次放行时 repair_feedback 为 None，抽取行为不变。
            documents = owner.extractor.extract(
                state["raw"], feedback=state.get("repair_feedback")
            )
        except Exception as exc:
            entry.status = "failed"
            entry.error = f"extract: {type(exc).__name__}: {exc}"
            logger.exception("抽取失败: %s", path)
            return {
                "documents": [],
                "queue": [],
                "failures": [
                    {"file": path.name, "stage": "extract", "error": entry.error}
                ],
            }
        entry.documents = len(documents)
        queue = [(doc, charge) for doc in documents for charge in doc.charges]
        return {"documents": documents, "queue": queue}

    def synthesize(state: BillingState) -> dict[str, Any]:
        """段3 综合（票04：综合 agent ReAct 工作台，只产提案不落账）。

        对当前费用：单据级校验（每单一次）+ 重复 lookup 后，交综合 agent
        自主调工具（认亲/读原文/查台账/查字典）产提案，再经死规则装配。
        未注入 agent LLM 时沿用确定性入账链，行为与票02 一致。
        """
        doc, charge = state["queue"][0]
        doc_issues = dict(state.get("doc_issues") or {})
        if id(doc) not in doc_issues:
            doc_issues[id(doc)] = validate_document(
                doc, tolerance_ratio=settings.total_tolerance_ratio
            )
        payload = {
            "document": doc,
            "charge": charge,
            "doc_issues": doc_issues[id(doc)],
            "duplicate_of": owner._lookup_duplicate(doc, charge),
            "settings": settings,
        }
        try:
            assessment = synthesize_charge(owner, payload, raw=state.get("raw"))
        except Exception as exc:
            logger.exception("记录装配失败: %s", state["path"])
            return {
                "doc_issues": doc_issues,
                "assessment": None,
                "charge_error": f"{type(exc).__name__}: {exc}",
            }
        return {
            "doc_issues": doc_issues,
            "assessment": assessment,
            "charge_error": None,
        }

    def tail(state: BillingState) -> dict[str, Any]:
        """段4 死规则尾巴：把关落账。

        提案可用 → 落账前钱关键项确定性交叉验证（票05，复用校验/认亲/
        四要素）→ 通过才落账 + 登记重复 + 收集记录；任一不一致/不可信 →
        降级 PENDING_REVIEW，错提案不落账。装配失败 → 记 assemble 失败并跳过。
        """
        doc, charge = state["queue"][0]
        entry = state["entry"]
        records = list(state.get("records") or [])
        assessments = list(state.get("assessments") or [])
        failures = list(state.get("failures") or [])
        assessment = state.get("assessment")
        if assessment is None:
            failures.append(
                {
                    "file": state["path"].name,
                    "stage": "assemble",
                    "error": state.get("charge_error"),
                    "charge": charge.charge_item,
                }
            )
        else:
            # 票05：落账前对钱关键项确定性交叉验证，对确定性/agent 两路一致生效。
            assessment = crosscheck_assessment(owner, assessment, doc, charge)
            owner._apply_to_ledger(assessment, doc, charge)
            owner._register_duplicate(doc, charge, assessment)
            records.append(assessment.record)
            assessments.append(assessment)
            entry.records += 1
            entry.operations[assessment.operation] = (
                entry.operations.get(assessment.operation, 0) + 1
            )
        return {
            "queue": state["queue"][1:],
            "records": records,
            "assessments": assessments,
            "failures": failures,
            "assessment": None,
            "charge_error": None,
        }

    # ------------------------------------------------------------------ #
    # 条件边路由（票03/04 的接入点）
    # ------------------------------------------------------------------ #
    def route_after_ingest(state: BillingState) -> str:
        """读进来之后：硬失败隔离（PENDING_REVIEW 已在 ingest 产出）；成功进闸门。"""
        if state["entry"].status == "failed" or state.get("raw") is None:
            return END
        return "gate"

    def route_after_gate(state: BillingState) -> str:
        """闸门之后：放行→extract；软失败且重试<上限→repair；耗尽→pending_review。"""
        if state.get("gate_passed", True):
            return "extract"
        if state.get("retries", 0) < settings.ingest_max_retries:
            return "repair"
        return "pending_review"

    def route_after_extract(state: BillingState) -> str:
        """双路抽取之后：失败隔离；无费用可处理时直接收尾。"""
        if state["entry"].status == "failed" or not state.get("queue"):
            return END
        return "synthesize"

    def route_after_tail(state: BillingState) -> str:
        """尾巴之后：还有待处理费用则回到综合段，逐条循环。"""
        return "synthesize" if state.get("queue") else END

    # ------------------------------------------------------------------ #
    # 装配
    # ------------------------------------------------------------------ #
    graph = StateGraph(BillingState)
    graph.add_node("ingest", ingest)
    graph.add_node("gate", gate)
    graph.add_node("repair", repair)
    graph.add_node("pending_review", pending_review)
    graph.add_node("extract", extract)
    graph.add_node("synthesize", synthesize)
    graph.add_node("tail", tail)
    graph.add_edge(START, "ingest")
    graph.add_conditional_edges(
        "ingest", route_after_ingest, {"gate": "gate", END: END}
    )
    graph.add_conditional_edges(
        "gate",
        route_after_gate,
        {"extract": "extract", "repair": "repair", "pending_review": "pending_review"},
    )
    graph.add_edge("repair", "ingest")
    graph.add_edge("pending_review", END)
    graph.add_conditional_edges(
        "extract", route_after_extract, {"synthesize": "synthesize", END: END}
    )
    graph.add_edge("synthesize", "tail")
    graph.add_conditional_edges(
        "tail", route_after_tail, {"synthesize": "synthesize", END: END}
    )
    return graph.compile()


__all__ = ["RECURSION_LIMIT", "build_billing_graph"]