"""四段式 StateGraph 装配：读进来 → 双路抽取 → 综合 → 尾巴。

票02"搬骨架不改行为"：四段全部仍是死规则。综合段暂用现有确定性逻辑
占位（单据校验 + 重复 lookup + 认亲 → 记账方向 → 校验 → 装配），
票04 换成综合 agent（ReAct 工作台）；尾巴负责把关落账，票05 在落账前
加钱关键项确定性交叉验证。

条件边预留：
* ingest / extract 之后：失败隔离路由，票03 在此接入软失败闸门与
  解析补救循环；
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
from .state import BillingState

if TYPE_CHECKING:
    from ..pipeline import BillingAgent

logger = get_logger("graph")

#: 逐条循环每条费用消耗 2 个超步；放宽递归上限，避免多费用单据
#: 触发 LangGraph 默认 25 超步上限（旧循环没有该限制）。
RECURSION_LIMIT = 10000


def build_billing_graph(owner: "BillingAgent"):
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
        lambda path: load_document(
            path, ocr_client=owner.ocr, work_dir=settings.work_dir
        )
    )

    # ------------------------------------------------------------------ #
    # 四段节点
    # ------------------------------------------------------------------ #
    def ingest(state: BillingState) -> dict[str, Any]:
        """段1 读进来：解析文件（EML/PDF/图片/Excel/Word，附件递归，带重试）。"""
        path = state["path"]
        entry = state["entry"]
        try:
            raw = load_with_retry(path)
        except Exception as exc:  # noqa: BLE001 - 单文件失败不影响整批
            entry.status = "failed"
            entry.error = f"{type(exc).__name__}: {exc}"
            logger.exception("解析失败: %s", path)
            return {
                "raw": None,
                "failures": [
                    {"file": path.name, "stage": "ingest", "error": entry.error}
                ],
            }
        entry.kind = raw.kind
        entry.ocr_source = raw.ocr_source
        entry.warnings = list(raw.warnings)
        return {"raw": raw}

    def extract(state: BillingState) -> dict[str, Any]:
        """段2 双路抽取：规则草稿 ∥ LLM 草稿 → 融合 → 标准化。

        双路链与附件递归仍在 DocumentExtractor 内（build_document_chain），
        本节点只负责把产出的单据排成逐条费用队列。
        """
        path = state["path"]
        entry = state["entry"]
        try:
            documents = owner.extractor.extract(state["raw"])
        except Exception as exc:  # noqa: BLE001
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
        """段3 综合（票02 占位：现有确定性逻辑，只产提案不落账）。

        对当前费用：单据级校验（每单一次）+ 重复 lookup +
        认亲 → 记账方向 → 校验 → 装配。票04 将换成综合 agent。
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
            assessment = owner.record_chain.invoke(payload)
        except Exception as exc:  # noqa: BLE001 - 单条费用失败不影响其余费用
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

        提案可用 → 落账 + 登记重复 + 收集记录；装配失败 → 记 assemble
        失败并跳过该条。票05 将在落账前加钱关键项确定性交叉验证。
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
        """读进来之后：失败隔离。票03 在此加软失败闸门与补救循环。"""
        return END if state["entry"].status == "failed" else "extract"

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
    graph.add_node("extract", extract)
    graph.add_node("synthesize", synthesize)
    graph.add_node("tail", tail)
    graph.add_edge(START, "ingest")
    graph.add_conditional_edges(
        "ingest", route_after_ingest, {"extract": "extract", END: END}
    )
    graph.add_conditional_edges(
        "extract", route_after_extract, {"synthesize": "synthesize", END: END}
    )
    graph.add_edge("synthesize", "tail")
    graph.add_conditional_edges(
        "tail", route_after_tail, {"synthesize": "synthesize", END: END}
    )
    return graph.compile()


__all__ = ["RECURSION_LIMIT", "build_billing_graph"]