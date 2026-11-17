"""PDF 解析：文本层优先，扫描件转 OCR。

为什么不是"PDF 一律走 OCR"？
* 文本层是原始矢量文字，准确率 100%，且不受网络/服务波动影响；
* 扫描件（文本层字符数过少）才需要 OCR；
* 通过 ``--ocr-mode api`` 可以强制所有 PDF 走 OCR，用于验收"调用 OCR API"。
"""

from __future__ import annotations

import re
from pathlib import Path

from ..io_utils import get_logger, normalize_text, sha256_bytes
from ..models import RawDocument, RawTable
from .ocr_client import OcrClient

logger = get_logger("pdf")

# 与 extract.rows.parse_charge_row 的行长上限一致：超过该长度的行会被费用解析丢弃。
_FUSED_LINE_LIMIT = 220
# 费用表粘连信号：表头 DESCRIPTION 与 AMOUNT/RATE/DEBIT 被压进同一行。
_FUSED_CHARGE_SIGNAL = re.compile(r"DESCRIPTION.{0,40}(?:AMOUNT|RATE|DEBIT)", re.IGNORECASE)


def _fused_charge_table(text: str) -> bool:
    """判断 plain 抽取是否把费用表粘成了超长整行。

    部分 PDF 的文本层在 pypdf plain 模式下会把表头、多行费用与 TOTAL
    压成一行（单号也会吞掉相邻标签）。此时按行解析全部失效，需要改用
    layout 模式重抽该页。信号要求"超长行 + 费用表头同现"，避免误伤
    普通长段落（如免责条款）。
    """
    return any(
        len(line) > _FUSED_LINE_LIMIT and _FUSED_CHARGE_SIGNAL.search(line)
        for line in text.splitlines()
    )


def _extract_with_pypdf(raw_bytes: bytes) -> tuple[list[str], str | None]:
    try:
        import io

        import pypdf
    except ImportError:  # pragma: no cover
        return [], "未安装 pypdf，无法读取 PDF 文本层"

    try:
        reader = pypdf.PdfReader(io.BytesIO(raw_bytes))
        pages: list[str] = []
        for page in reader.pages:
            try:
                text = normalize_text(page.extract_text() or "")
                # 仅当检测到费用表被粘成超长整行时，才对该页改用 layout 模式重抽。
                # 不能全局用 layout：部分 PDF 在 layout 下金额会粘到描述上，反而解析不出。
                if _fused_charge_table(text):
                    text = normalize_text(page.extract_text(extraction_mode="layout") or "")
                pages.append(text)
            except Exception as exc:  # noqa: BLE001
                pages.append("")
                logger.debug("单页解析失败: %s", exc)
        return pages, None
    except Exception as exc:  # noqa: BLE001
        return [], f"pypdf 解析失败: {type(exc).__name__}: {exc}"


def parse_pdf(
    path: Path, *, ocr_client: OcrClient, role: str = "primary", force_ocr: bool = False
) -> RawDocument:
    settings = ocr_client.settings
    raw_bytes = path.read_bytes()
    sha = sha256_bytes(raw_bytes)
    warnings: list[str] = []

    if settings.ocr_mode == "api":
        pages, err = [], "强制 OCR 模式"
    else:
        pages, err = _extract_with_pypdf(raw_bytes)
        if err:
            warnings.append(err)

    text = "\n\n".join(p for p in pages if p)
    ocr_source: str | None = "pdf_text_layer" if text else None
    ocr_confidence: float | None = 1.0 if text else None
    tables: list[RawTable] = []

    # force_ocr：票03 解析补救"文本层空/脏→强制重 OCR"策略入口；
    # 其余情况仍按文本层字符数阈值决定是否转 OCR（不改默认行为）。
    need_ocr = (
        force_ocr
        or settings.ocr_mode == "api"
        or len(text) < settings.pdf_text_layer_min_chars
    )
    if need_ocr:
        outcome = ocr_client.process_file(path)
        if outcome.text or outcome.tables:
            ocr_text = normalize_text(outcome.text)
            if ocr_text:
                text = ocr_text
                pages = [ocr_text]
                ocr_source = outcome.source
                ocr_confidence = outcome.confidence
            tables.extend(outcome.tables)
            warnings.extend(outcome.warnings)
        else:
            warnings.extend(outcome.warnings or ["OCR 未返回内容"])
            if text:
                warnings.append("OCR 未返回内容，使用 PDF 文本层结果")
            else:
                warnings.append("PDF 无可读取文本，且 OCR 未返回内容")
    elif settings.ocr_mode in {"cache", "auto"}:
        # 文本层足够时，仍读取 OCR 缓存里的表格（若曾经识别过），用于结构化补强
        cached = ocr_client._load_cache(sha)
        if cached and cached.tables:
            tables.extend(cached.tables)

    return RawDocument(
        doc_id=sha[:12],
        path=str(path),
        filename=path.name,
        kind="pdf",
        sha256=sha,
        size=len(raw_bytes),
        text=text,
        pages=pages,
        tables=tables,
        metadata={
            "page_count": len(pages),
            "ocr_source": ocr_source,
            "has_text_layer": bool(err is None and text),
        },
        warnings=warnings,
        ocr_source=ocr_source,
        ocr_confidence=ocr_confidence,
    )


__all__ = ["parse_pdf"]
