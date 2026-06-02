# Copyright (c) Opendatalab. All rights reserved.
"""
RAG 后端 — 文件类型自动路由。

检测输入文件类型, 自动分发到对应的处理链路:
- .xlsx/.xls → Excel 处理链路
- .pdf/图片  → PDF RAG 处理链路 (Azure DI + Dify)
- .docx/.pptx → Office 原生后端 (已有)

使用方式:
  from mineru.backend.rag.common import dispatch_by_type
  results = await dispatch_by_type("/path/to/input", "./output")
"""
import asyncio
import json
import os
from pathlib import Path
from typing import Optional

from loguru import logger

from mineru.data.data_reader_writer import FileBasedDataWriter
from mineru.utils.enum_class import MakeMode


# ── 文件类型定义 ──────────────────────────────────────────

PDF_SUFFIXES = {"pdf"}
IMAGE_SUFFIXES = {"png", "jpeg", "jp2", "webp", "gif", "bmp", "jpg", "tiff"}
EXCEL_SUFFIXES = {"xlsx", "xls", "xlsm", "xltx", "csv"}
DOCX_SUFFIXES = {"docx"}
PPTX_SUFFIXES = {"pptx"}


def detect_file_type(file_path: Path) -> str:
    """
    检测文件类型。

    Returns:
        "pdf" | "image" | "excel" | "docx" | "pptx" | "unknown"
    """
    suffix = file_path.suffix.lower().lstrip(".")
    if suffix in PDF_SUFFIXES:
        return "pdf"
    if suffix in IMAGE_SUFFIXES:
        return "image"
    if suffix in EXCEL_SUFFIXES:
        return "excel"
    if suffix in DOCX_SUFFIXES:
        return "docx"
    if suffix in PPTX_SUFFIXES:
        return "pptx"
    return "unknown"


def collect_files_by_type(input_path: Path) -> dict[str, list[Path]]:
    """
    扫描输入路径, 按文件类型分组。

    Returns:
        {"pdf": [...], "excel": [...], "image": [...], ...}
    """
    groups: dict[str, list[Path]] = {}

    if input_path.is_file():
        ft = detect_file_type(input_path)
        groups[ft] = [input_path]
    elif input_path.is_dir():
        for path in sorted(input_path.glob("*")):
            if path.is_file():
                ft = detect_file_type(path)
                if ft == "unknown":
                    continue
                groups.setdefault(ft, []).append(path)
    else:
        raise FileNotFoundError(f"Input path does not exist: {input_path}")

    # 统计
    total = sum(len(v) for v in groups.values())
    summary = ", ".join(f"{ft}: {len(files)}" for ft, files in sorted(groups.items()))
    logger.info(f"文件扫描: {total} 个文件 → {summary}")

    return groups


# ── 分发 ──────────────────────────────────────────────────

async def dispatch_by_type(
    input_path: str | Path,
    output_dir: str | Path,
    **params,
) -> dict:
    """
    根据文件类型自动分发到对应的处理器。

    Args:
        input_path: 输入文件或目录路径
        output_dir: 输出目录
        **params: 透传到各处理器的参数
            - start_page_id, end_page_id: PDF 页范围
            - lang, parse_method, formula_enable, table_enable: PDF 参数
            - use_dify: 是否启用 Dify (默认 True)

    Returns:
        {
            "output_dir": str,
            "results": {
                "pdf": [...],
                "excel": [...],
                ...
            },
            "summary": str,
        }
    """
    input_path = Path(input_path)
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    file_groups = collect_files_by_type(input_path)

    if not file_groups:
        raise ValueError(f"No supported files found in {input_path}")

    all_results: dict[str, list[dict]] = {}
    use_dify = params.pop("use_dify", True)

    # ── PDF + 图片 → RAG Pipeline ──
    pdf_files = file_groups.get("pdf", []) + file_groups.get("image", [])
    if pdf_files:
        logger.info(f"分发 PDF/图片 → RAG Pipeline: {len(pdf_files)} 个文件")
        pdf_results = await _dispatch_pdf(pdf_files, output_dir, use_dify, params)
        all_results["pdf"] = pdf_results

    # ── Excel → Excel Processor ──
    excel_files = file_groups.get("excel", [])
    if excel_files:
        logger.info(f"分发 Excel → Excel Processor: {len(excel_files)} 个文件")
        excel_results = await _dispatch_excel(excel_files, output_dir, use_dify)
        all_results["excel"] = excel_results

    # ── DOCX/PPTX → 委托 Office 后端 ──
    office_files = file_groups.get("docx", []) + file_groups.get("pptx", [])
    if office_files:
        logger.info(f"分发 Office → Native Backend: {len(office_files)} 个文件")
        office_results = await _dispatch_office(office_files, output_dir, params)
        all_results["office"] = office_results

    # ── 汇总 ──
    total = sum(len(v) for v in all_results.values())
    summary_parts = []
    for ft, results in sorted(all_results.items()):
        success = sum(1 for r in results if not r.get("error"))
        summary_parts.append(f"{ft}: {success}/{len(results)}")

    logger.info(f"分发完成: {', '.join(summary_parts)}")

    return {
        "output_dir": str(output_dir),
        "results": all_results,
        "summary": f"{total} files → " + ", ".join(summary_parts),
    }


async def _dispatch_pdf(
    files: list[Path],
    output_dir: Path,
    use_dify: bool,
    params: dict,
) -> list[dict]:
    """分发 PDF/图片到 RAG Pipeline"""
    from mineru.backend.rag.pipeline.chain import default_rag_chain
    from mineru.backend.rag.rag_analyze import aio_doc_analyze_chain

    chain = default_rag_chain()
    if not use_dify:
        chain.disable("dify_enhance")

    results = []
    for path in files:
        try:
            pdf_bytes = path.read_bytes()
            middle_json, model_output = await aio_doc_analyze_chain(
                pdf_bytes=pdf_bytes,
                output_dir=str(output_dir),
                doc_stem=path.stem,
                chain=chain,
                **params,
            )
            _write_output(output_dir, path.stem, "rag", middle_json, model_output, None)
            results.append({"file": str(path), "status": "ok"})
        except Exception as e:
            logger.exception(f"PDF 处理失败: {path}")
            results.append({"file": str(path), "status": "failed", "error": str(e)})

    return results


async def _dispatch_excel(
    files: list[Path],
    output_dir: Path,
    use_dify: bool,
) -> list[dict]:
    """分发 Excel 到 Excel Processor"""
    from mineru.backend.rag.excel_processor import parse_excel_to_markdown

    results = []
    for path in files:
        try:
            await parse_excel_to_markdown(
                file_path=str(path),
                output_dir=str(output_dir),
            )
            results.append({"file": str(path), "status": "ok"})
        except Exception as e:
            logger.exception(f"Excel 处理失败: {path}")
            results.append({"file": str(path), "status": "failed", "error": str(e)})

    return results


async def _dispatch_office(
    files: list[Path],
    output_dir: Path,
    params: dict,
) -> list[dict]:
    """分发 DOCX/PPTX 到 Office 原生后端"""
    import asyncio as _asyncio

    from mineru.cli.common import (
        prepare_env, office_suffixes, docx_suffixes, pptx_suffixes,
    )
    from mineru.backend.office.docx_analyze import office_docx_analyze
    from mineru.backend.office.pptx_analyze import office_pptx_analyze
    from mineru.backend.office.office_middle_json_mkcontent import union_make
    from mineru.backend.rag.rag_middle_json_mkcontent import union_make as rag_union_make

    results = []
    for path in files:
        try:
            suffix = path.suffix.lower().lstrip(".")
            file_bytes = path.read_bytes()

            local_image_dir, local_md_dir = prepare_env(
                str(output_dir), path.stem, "office"
            )
            image_writer = FileBasedDataWriter(local_image_dir)
            md_writer = FileBasedDataWriter(local_md_dir)

            # 调用 Office 后端
            if suffix in docx_suffixes:
                middle_json, infer_result = await _asyncio.to_thread(
                    office_docx_analyze, file_bytes, image_writer,
                )
            elif suffix in pptx_suffixes:
                middle_json, infer_result = await _asyncio.to_thread(
                    office_pptx_analyze, file_bytes, image_writer,
                )
            else:
                raise ValueError(f"Unsupported office suffix: {suffix}")

            pdf_info = middle_json["pdf_info"]
            img_dir = os.path.basename(local_image_dir)

            # Markdown
            md = union_make(pdf_info, MakeMode.MM_MD, img_dir)
            md_writer.write_string(f"{path.stem}.md", md)

            # Content List
            cl = union_make(pdf_info, MakeMode.CONTENT_LIST, img_dir)
            md_writer.write_string(
                f"{path.stem}_content_list.json",
                json.dumps(cl, ensure_ascii=False, indent=4),
            )

            # Middle JSON
            md_writer.write_string(
                f"{path.stem}_middle.json",
                json.dumps(middle_json, ensure_ascii=False, indent=4),
            )

            results.append({"file": str(path), "status": "ok"})
        except Exception as e:
            logger.exception(f"Office 处理失败: {path}")
            results.append({"file": str(path), "status": "failed", "error": str(e)})

    return results


# ── 输出写入 ──────────────────────────────────────────────

def _write_output(
    output_dir: Path,
    doc_stem: str,
    method: str,
    middle_json: dict,
    model_output: dict,
    image_dir: Optional[str] = None,
) -> None:
    """写入标准输出文件 (Markdown + JSON)"""
    from mineru.cli.common import prepare_env
    from mineru.backend.rag.rag_middle_json_mkcontent import union_make

    local_image_dir, local_md_dir = prepare_env(str(output_dir), doc_stem, method)
    md_writer = FileBasedDataWriter(local_md_dir)
    img_dir = image_dir or os.path.basename(local_image_dir)
    pdf_info = middle_json["pdf_info"]

    md_writer.write_string(f"{doc_stem}.md",
                           union_make(pdf_info, MakeMode.MM_MD, img_dir))
    md_writer.write_string(f"{doc_stem}_content_list.json",
                           json.dumps(union_make(pdf_info, MakeMode.CONTENT_LIST, img_dir),
                                      ensure_ascii=False, indent=4))
    md_writer.write_string(f"{doc_stem}_middle.json",
                           json.dumps(middle_json, ensure_ascii=False, indent=4))
    md_writer.write_string(f"{doc_stem}_model.json",
                           json.dumps(model_output, ensure_ascii=False, indent=4))


# ── 同步入口 (兼容原有 do_parse 签名) ───────────────────

def _process_rag(
    output_dir,
    pdf_file_names: list[str],
    pdf_bytes_list: list[bytes],
    lang_list: list[str],
    parse_method: str = "auto",
    formula_enable: bool = True,
    table_enable: bool = True,
    **kwargs,
) -> None:
    """同步版 RAG 处理入口 (兼容 do_parse 调用)"""
    asyncio.run(
        _dispatch_by_bytes(
            output_dir, pdf_file_names, pdf_bytes_list, lang_list,
            parse_method, formula_enable, table_enable, **kwargs,
        )
    )


async def _dispatch_by_bytes(
    output_dir,
    pdf_file_names: list[str],
    pdf_bytes_list: list[bytes],
    lang_list: list[str],
    parse_method: str = "auto",
    formula_enable: bool = True,
    table_enable: bool = True,
    **kwargs,
) -> None:
    """按字节流分发 (API 内部使用)"""
    from mineru.backend.rag.rag_analyze import aio_doc_analyze_chain
    from mineru.backend.rag.pipeline.chain import default_rag_chain

    chain = default_rag_chain()
    if not kwargs.get("use_dify", True):
        chain.disable("dify_enhance")

    for idx, (pdf_bytes, pdf_file_name, lang) in enumerate(
        zip(pdf_bytes_list, pdf_file_names, lang_list)
    ):
        logger.info(f"[{idx + 1}/{len(pdf_bytes_list)}] {pdf_file_name}")

        middle_json, model_output = await aio_doc_analyze_chain(
            pdf_bytes=pdf_bytes,
            output_dir=str(output_dir),
            doc_stem=pdf_file_name,
            chain=chain,
            lang=lang,
            parse_method=parse_method,
            formula_enable=formula_enable,
            table_enable=table_enable,
            **kwargs,
        )

        _write_output(
            Path(output_dir), pdf_file_name, f"rag_{parse_method}",
            middle_json, model_output,
        )


# ── __main__ ──────────────────────────────────────────────

if __name__ == "__main__":
    import sys

    async def _main():
        if len(sys.argv) < 3:
            print("Usage: python -m mineru.backend.rag.common <input_path> <output_dir>")
            sys.exit(1)

        result = await dispatch_by_type(sys.argv[1], sys.argv[2])
        print(f"Done: {result['summary']}")

    asyncio.run(_main())
