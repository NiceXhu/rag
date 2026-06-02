# Copyright (c) Opendatalab. All rights reserved.
"""
将 Azure DI + Dify 的增强结果转换为 MinerU 标准的 middle_json 格式。

遵循与 pipeline/vlm 后端相同的 middle_json 结构:
{
    "pdf_info": [
        {
            "preproc_blocks": [...],
            "page_idx": 0,
            "page_size": [w, h],
            "discarded_blocks": [...],
        }
    ],
    "_backend": "rag",
    "_version_name": "...",
}
"""
import copy
import time

from loguru import logger

from mineru.backend.rag.rag_magic_model import RAGMagicModel
from mineru.utils.enum_class import BlockType, ContentType
from mineru.version import __version__


def init_middle_json() -> dict:
    """初始化 middle_json 结构"""
    return {"pdf_info": [], "_backend": "rag", "_version_name": __version__}


def page_analysis_to_page_info(
    page_analysis: dict,
    dify_image_results: list,
    dify_table_results: list,
    page_number: int = 0,
    page_width: int = 0,
    page_height: int = 0,
) -> dict:
    """将单页的 Azure DI 分析结果 + Dify 增强结果 转为标准 page_info 结构"""
    magic_model = RAGMagicModel(
        page_analysis=page_analysis,
        dify_image_results=dify_image_results,
        dify_table_results=dify_table_results,
        page_number=page_number,
        page_width=page_width or page_analysis.get("width", 0),
        page_height=page_height or page_analysis.get("height", 0),
    )

    preproc_blocks = magic_model.get_preproc_blocks()
    discarded_blocks = magic_model.get_discarded_blocks()

    return {
        "preproc_blocks": preproc_blocks,
        "page_idx": page_number,
        "page_size": [
            page_analysis.get("width", page_width),
            page_analysis.get("height", page_height),
        ],
        "discarded_blocks": discarded_blocks,
        "_rag_metadata": {
            "dify_enhanced_images": len([r for r in dify_image_results if r.description]),
            "dify_enhanced_tables": len([r for r in dify_table_results if r.optimized_html]),
        },
    }


def append_page_results_to_middle_json(
    middle_json: dict,
    page_results: list[dict],
    dify_image_results: list,
    dify_table_results: list,
    page_start_index: int = 0,
    progress_bar=None,
) -> None:
    """
    将批量页面分析结果追加到 middle_json 中。

    类似于 pipeline 的 append_batch_results_to_middle_json，
    将 Azure DI + Dify 的结果逐一转为 page_info 并追加。
    """
    for offset, page_result in enumerate(page_results):
        page_info = page_analysis_to_page_info(
            page_analysis=page_result,
            dify_image_results=[
                r for r in dify_image_results
                if hasattr(r, 'page_number') and r.page_number == page_start_index + offset
            ],
            dify_table_results=[
                r for r in dify_table_results
                if hasattr(r, 'page_number') and r.page_number == page_start_index + offset
            ],
            page_number=page_start_index + offset,
        )

        if page_info is None:
            page_info = {
                "preproc_blocks": [],
                "page_idx": page_start_index + offset,
                "page_size": [0, 0],
                "discarded_blocks": [],
            }

        middle_json["pdf_info"].append(page_info)

        if progress_bar is not None:
            progress_bar.update(1)


def finalize_middle_json(pdf_info_list: list[dict]) -> None:
    """
    对整个文档的 middle_json 进行后处理。

    RAG 后端的后处理包括:
    - 跨页表格合并 (从 pipeline 继承)
    - 阅读顺序全局优化
    """
    from mineru.backend.utils.runtime_utils import cross_page_table_merge

    # 跨页表格合并
    cross_page_table_merge(pdf_info_list)

    logger.debug(f"RAG middle_json finalized: {len(pdf_info_list)} pages")


def build_model_output(
    azure_result: object,
    dify_image_results: list,
    dify_table_results: list,
) -> dict:
    """
    构建 model_output (原始模型输出，用于调试和追溯)

    包含:
    - Azure DI 原始结果摘要
    - Dify 增强结果列表
    - 处理时间戳
    """
    output = {
        "backend": "rag",
        "azure_di": {
            "page_count": getattr(azure_result, 'page_count', 0) if azure_result else 0,
            "metadata": getattr(azure_result, 'metadata', {}) if azure_result else {},
        },
        "dify_enhancement": {
            "image_count": len(dify_image_results),
            "table_count": len(dify_table_results),
            "enhanced_images": [
                {
                    "image_key": r.image_key if hasattr(r, 'image_key') else "",
                    "page_number": r.page_number if hasattr(r, 'page_number') else 0,
                    "description_length": len(r.description) if hasattr(r, 'description') else 0,
                    "category": r.category if hasattr(r, 'category') else "unknown",
                }
                for r in dify_image_results
            ],
            "enhanced_tables": [
                {
                    "table_index": r.table_index if hasattr(r, 'table_index') else 0,
                    "page_number": r.page_number if hasattr(r, 'page_number') else 0,
                    "has_optimized_md": bool(r.optimized_markdown) if hasattr(r, 'optimized_markdown') else False,
                }
                for r in dify_table_results
            ],
        },
        "timestamp": time.time(),
    }
    return output
