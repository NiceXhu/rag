# Copyright (c) Opendatalab. All rights reserved.
"""
RAG 数据处理 Pipeline — 主编排器。

整合 Azure Document Intelligence OCR + Dify Workflow 增强的完整处理流水线。

处理流程 (每阶段有 checkpoint 缓存):
1. 原始 PDF 字节流 → Azure Document Intelligence (一次调用, 服务端自动并行)
2. Azure DI 返回每页的结构化数据 → 按页分组
3. 图片/表格区域 → Dify Workflow (并发: 图片描述生成 + 表格优化)
4. Azure + Dify 结果 → RAGMagicModel 归类 → middle_json
5. middle_json → Markdown / JSON 最终输出

可观测:
- RAGPipelineTracker 统一追踪每个阶段 (时序/状态/缓存命中)
- 每阶段中间结果缓存到 {output}/.rag_cache/
- 失败后从 checkpoint 恢复, 不重复执行已完成阶段
- 自动生成可视化: layout 框线图、Dify 前后对比、时间线图
"""
import asyncio
from typing import Optional

from loguru import logger
from tqdm import tqdm

from mineru.backend.rag.azure_doc_intelligence import (
    AzureDocumentIntelligenceClient,
    DocumentAnalysisResult,
)
from mineru.backend.rag.dify_client import (
    DifyWorkflowClient,
    DifyImageResult,
    DifyTableResult,
)
from mineru.backend.rag.model_output_to_middle_json import (
    init_middle_json,
    append_page_results_to_middle_json,
    finalize_middle_json,
    build_model_output,
)
from mineru.backend.rag.observability import (
    RAGPipelineTracker,
    StageStatus,
)
from mineru.data.data_reader_writer import FileBasedDataWriter


# ── 并发配置 ────────────────────────────────────────────────
DIFY_CONCURRENT_CALLS = 8           # Dify 对同一批图片/表格的并发数
DIFY_IMAGE_BATCH_SIZE = 16          # Dify 图片每批并发数 (分批提交, 避免一次过多)
DIFY_TABLE_BATCH_SIZE = 16          # Dify 表格每批并发数


class RAGModelSingleton:
    """RAG Pipeline 客户端单例管理器"""

    _instance = None
    _clients = {}
    _lock = __import__('threading').RLock()

    def __new__(cls, *args, **kwargs):
        with cls._lock:
            if cls._instance is None:
                cls._instance = super().__new__(cls)
        return cls._instance

    def get_clients(self):
        with self._lock:
            if 'azure' not in self._clients:
                self._clients['azure'] = AzureDocumentIntelligenceClient()
            if 'dify' not in self._clients:
                self._clients['dify'] = DifyWorkflowClient()
        return self._clients['azure'], self._clients['dify']


# ── Azure DI 结果按页面分组 ──────────────────────────────

def _group_azure_results_by_page(
    azure_result: DocumentAnalysisResult,
    start_page_id: int = 0,
    end_page_id: Optional[int] = None,
) -> list[dict]:
    """
    将 Azure DI 的全量分析结果按页面重组。

    Azure DI 返回的每个元素 (paragraph/table/figure) 都携带
    bounding_regions[0].page_number, 由此分配到对应页面。

    Returns:
        [{page_number, width, height, paragraphs, tables, figures}, ...]
    """
    page_map: dict[int, dict] = {}

    for page in azure_result.pages:
        pn = page["page_number"]
        page_map[pn] = {
            "page_number": pn,
            "width": page.get("width", 0),
            "height": page.get("height", 0),
            "unit": page.get("unit", "inch"),
            "paragraphs": [],
            "tables": [],
            "figures": [],
        }

    for para in azure_result.paragraphs:
        pn = para.get("page_number")
        if pn is not None and pn in page_map:
            page_map[pn]["paragraphs"].append(para)

    for table in azure_result.tables:
        page_numbers = table.get("page_numbers", [])
        pn = page_numbers[0] if page_numbers else min(page_map.keys()) if page_map else None
        if pn is not None and pn in page_map:
            page_map[pn]["tables"].append(table)

    for figure in azure_result.figures:
        page_numbers = figure.get("page_numbers", [])
        pn = page_numbers[0] if page_numbers else None
        if pn is not None and pn in page_map:
            page_map[pn]["figures"].append(figure)

    sorted_pages = sorted(page_map.values(), key=lambda p: p["page_number"])
    end = end_page_id + 1 if end_page_id is not None else None
    if start_page_id > 0 or end is not None:
        sorted_pages = sorted_pages[start_page_id:end]

    logger.info(
        f"Azure DI results grouped: {len(sorted_pages)} pages, "
        f"{sum(len(p['paragraphs']) for p in sorted_pages)} paragraphs, "
        f"{sum(len(p['tables']) for p in sorted_pages)} tables, "
        f"{sum(len(p['figures']) for p in sorted_pages)} figures"
    )
    return sorted_pages


# ── Dify 任务构建 ─────────────────────────────────────────

def _build_dify_tasks(
    pages_data: list[dict],
    dify_client: DifyWorkflowClient,
    dify_sem: asyncio.Semaphore,
) -> tuple[list, list]:
    """遍历所有页面, 构建 Dify 图片/表格增强协程任务列表"""
    dify_image_tasks = []
    dify_table_tasks = []

    for page_data in pages_data:
        page_num = page_data["page_number"]
        context_text = " ".join(
            p.get("content", "") for p in page_data.get("paragraphs", [])
        )[:2000]

        for fig_idx, figure in enumerate(page_data.get("figures", [])):
            image_base64 = figure.get("image_base64", "")
            if not image_base64:
                continue

            # ★ 跳过被相关性过滤器标记的无意义图片
            if figure.get("_skip_dify"):
                continue

            async def _analyze(img_b64, key, pn, bb, ctx):
                async with dify_sem:
                    return await dify_client.analyze_image(
                        image_base64=img_b64, image_key=key,
                        page_number=pn, bbox=bb, context_text=ctx,
                    )

            dify_image_tasks.append(
                _analyze(image_base64, f"fig_p{page_num}_{fig_idx}",
                         page_num, figure.get("bbox"), context_text)
            )

        for tbl_idx, table in enumerate(page_data.get("tables", [])):
            table_html = table.get("table_html", "")
            if not table_html:
                continue

            cells = table.get("cells", [])
            table_bbox = None
            if cells:
                xs = [c["bbox"][0] for c in cells if c.get("bbox")]
                ys = [c["bbox"][1] for c in cells if c.get("bbox")]
                xs += [c["bbox"][2] for c in cells if c.get("bbox")]
                ys += [c["bbox"][3] for c in cells if c.get("bbox")]
                if xs and ys:
                    table_bbox = [min(xs), min(ys), max(xs), max(ys)]

            async def _optimize(html, idx, pn, bb, cap, ctx):
                async with dify_sem:
                    return await dify_client.optimize_table(
                        table_html=html, table_index=idx,
                        page_number=pn, bbox=bb, caption=cap, context_text=ctx,
                    )

            dify_table_tasks.append(
                _optimize(table_html, tbl_idx, page_num,
                          table_bbox, table.get("caption", ""), context_text)
            )

    return dify_image_tasks, dify_table_tasks


async def _execute_dify_enhancement(
    dify_image_tasks: list,
    dify_table_tasks: list,
    batch_size: int = DIFY_IMAGE_BATCH_SIZE,
) -> tuple[list[DifyImageResult], list[DifyTableResult]]:
    """
    分批并发执行 Dify 增强。

    优化: 将图片和表格任务合并为一个并发池, 而非先后串行处理。
    30 个图片 + 12 个表格 = 42 个任务共享 8 个并发槽,
    图片和表格不再互相等待。
    """
    all_image_results: list[DifyImageResult] = []
    all_table_results: list[DifyTableResult] = []

    # 合并为统一任务列表 (带类型标记)
    tagged_tasks = (
        [("image", t) for t in dify_image_tasks] +
        [("table", t) for t in dify_table_tasks]
    )

    if not tagged_tasks:
        return all_image_results, all_table_results

    for batch_start in range(0, len(tagged_tasks), batch_size):
        batch = tagged_tasks[batch_start:batch_start + batch_size]
        batch_coros = [t for _, t in batch]
        batch_tags = [tag for tag, _ in batch]

        results = await asyncio.gather(*batch_coros, return_exceptions=True)

        for tag, r in zip(batch_tags, results):
            if isinstance(r, Exception) or r is None:
                continue
            if tag == "image":
                all_image_results.append(r)
            else:
                all_table_results.append(r)

    failed_img = len(dify_image_tasks) - len(all_image_results)
    failed_tbl = len(dify_table_tasks) - len(all_table_results)
    if failed_img or failed_tbl:
        logger.warning(
            f"Dify: {len(all_image_results)} images, {len(all_table_results)} tables "
            f"({failed_img} image, {failed_tbl} table failures)"
        )

    return all_image_results, all_table_results


# ── 异步可视化辅助 (后台执行, 不阻塞主流程) ──────────────

async def _generate_layout_viz_async(
    tracker: RAGPipelineTracker,
    pages_data: list[dict],
    max_pages: int = 3,
) -> None:
    """后台生成 layout 框线可视化 (前几页)"""
    try:
        for page_data in pages_data[:max_pages]:
            page_num = page_data["page_number"]
            all_blocks = []

            for para in page_data.get("paragraphs", []):
                all_blocks.append({
                    "type": "paragraph",
                    "bbox": para.get("bbox"),
                    "content": para.get("content", ""),
                    "_page_width": page_data.get("width", 0),
                    "_page_height": page_data.get("height", 0),
                })

            for table in page_data.get("tables", []):
                cells = table.get("cells", [])
                if cells:
                    xs = [c["bbox"][0] for c in cells if c.get("bbox")]
                    ys = [c["bbox"][1] for c in cells if c.get("bbox")]
                    xs += [c["bbox"][2] for c in cells if c.get("bbox")]
                    ys += [c["bbox"][3] for c in cells if c.get("bbox")]
                    if xs and ys:
                        all_blocks.append({
                            "type": "table",
                            "bbox": [min(xs), min(ys), max(xs), max(ys)],
                            "_page_width": page_data.get("width", 0),
                            "_page_height": page_data.get("height", 0),
                        })

            for figure in page_data.get("figures", []):
                all_blocks.append({
                    "type": "figure",
                    "bbox": figure.get("bbox"),
                    "_page_width": page_data.get("width", 0),
                    "_page_height": page_data.get("height", 0),
                })

            if all_blocks:
                # 在线程池中执行同步的图片渲染
                await asyncio.to_thread(
                    tracker.draw_layout_boxes, page_num, all_blocks,
                )
    except Exception as e:
        logger.warning(f"[Viz] Layout visualization failed: {e}")


async def _generate_dify_viz_async(
    tracker: RAGPipelineTracker,
    image_results: list[DifyImageResult],
    table_results: list[DifyTableResult],
    max_items: int = 5,
) -> None:
    """后台生成 Dify 增强前后对比可视化"""
    try:
        for img_result in image_results[:max_items]:
            key = getattr(img_result, 'image_key', 'unknown')
            if getattr(img_result, 'description', ''):
                await asyncio.to_thread(
                    tracker.draw_dify_comparison,
                    original_text="",
                    enhanced_text=getattr(img_result, 'description', ''),
                    item_type="image",
                    item_key=key,
                )

        for tbl_result in table_results[:max_items]:
            idx = getattr(tbl_result, 'table_index', 0)
            pn = getattr(tbl_result, 'page_number', 0)
            if getattr(tbl_result, 'optimized_html', ''):
                await asyncio.to_thread(
                    tracker.draw_dify_comparison,
                    original_text="",
                    enhanced_text=getattr(tbl_result, 'optimized_html', ''),
                    item_type="table",
                    item_key=f"p{pn}_t{idx}",
                )
    except Exception as e:
        logger.warning(f"[Viz] Dify comparison visualization failed: {e}")


async def _generate_timeline_async(tracker: RAGPipelineTracker) -> None:
    """后台生成 Pipeline 时间线图"""
    try:
        await asyncio.to_thread(tracker.draw_pipeline_timeline)
    except Exception as e:
        logger.warning(f"[Viz] Timeline generation failed: {e}")


# ── 主入口 ────────────────────────────────────────────────

def doc_analyze(
    pdf_bytes: bytes,
    image_writer: Optional[FileBasedDataWriter] = None,
    output_dir: Optional[str] = None,
    doc_stem: str = "document",
    lang: str = "",
    parse_method: str = "auto",
    formula_enable: bool = True,
    table_enable: bool = True,
    start_page_id: int = 0,
    end_page_id: Optional[int] = None,
    **kwargs,
) -> tuple[dict, dict]:
    """同步版 RAG 文档分析入口"""
    return asyncio.run(
        aio_doc_analyze(
            pdf_bytes=pdf_bytes, image_writer=image_writer,
            output_dir=output_dir, doc_stem=doc_stem,
            lang=lang, parse_method=parse_method,
            formula_enable=formula_enable, table_enable=table_enable,
            start_page_id=start_page_id, end_page_id=end_page_id,
            **kwargs,
        )
    )


async def aio_doc_analyze(
    pdf_bytes: bytes,
    image_writer: Optional[FileBasedDataWriter] = None,
    output_dir: Optional[str] = None,
    doc_stem: str = "document",
    lang: str = "",
    parse_method: str = "auto",
    formula_enable: bool = True,
    table_enable: bool = True,
    start_page_id: int = 0,
    end_page_id: Optional[int] = None,
    **kwargs,
) -> tuple[dict, dict]:
    """
    RAG Pipeline 核心异步实现 (带可观测 + 缓存)。

    每个阶段都有 checkpoint, 失败后从缓存恢复:

    Stage 1 — Azure DI        ──→ checkpoint: azure_result
    Stage 2 — 页面分组          ──→ checkpoint: pages_data
    Stage 3 — Dify 增强         ──→ checkpoint: dify_results
    Stage 4 — middle_json 构建  ──→ checkpoint: middle_json
    """
    if image_writer is None:
        image_writer = FileBasedDataWriter("")

    # ── 0. 初始化追踪器 ─────────────────────────────
    tracker = RAGPipelineTracker(
        output_dir=output_dir or ".",
        doc_stem=doc_stem,
        pdf_bytes=pdf_bytes,
        params={
            "lang": lang, "parse_method": parse_method,
            "formula_enable": formula_enable, "table_enable": table_enable,
            "start_page_id": start_page_id, "end_page_id": end_page_id,
        },
    )
    tracker.start()

    try:
        model_manager = RAGModelSingleton()
        azure_client, dify_client = model_manager.get_clients()

        # ★ 页范围优化: 如果只需部分页面, 先裁剪 PDF
        effective_pdf_bytes = pdf_bytes
        if start_page_id > 0 or end_page_id is not None:
            from mineru.utils.pdfium_guard import rewrite_pdf_bytes_with_pdfium
            effective_pdf_bytes = rewrite_pdf_bytes_with_pdfium(
                pdf_bytes,
                start_page_id=start_page_id,
                end_page_id=end_page_id,
            )
            logger.info(
                f"[Pipeline] PDF trimmed: {len(pdf_bytes)} → {len(effective_pdf_bytes)} bytes "
                f"(pages [{start_page_id}, {end_page_id}])"
            )

        # ★ 跳过 Dify 检查
        dify_available = dify_client.is_configured

        # ── 1. Azure DI 分析 ─────────────────────────
        if tracker.has_checkpoint("azure_result"):
            logger.info("[Pipeline] Stage 1/4: Azure DI → cache hit")
            tracker.start_stage("azure_di", input_summary={"from": "cache"})
            azure_result = tracker.load_checkpoint("azure_result")
            tracker.end_stage("azure_di",
                output_summary={"pages": len(azure_result["pages"]), "from": "cache"},
                from_cache=True)
        else:
            logger.info("[Pipeline] Stage 1/4: Azure DI → analyzing...")
            tracker.start_stage("azure_di",
                input_summary={"pdf_bytes": len(effective_pdf_bytes)})

            azure_raw = await azure_client.analyze_document(
                file_bytes=effective_pdf_bytes, content_type="application/pdf",
            )

            # 转为可 JSON 序列化的 dict 后缓存
            azure_result = {
                "pages": azure_raw.pages,
                "paragraphs": azure_raw.paragraphs,
                "tables": azure_raw.tables,
                "figures": azure_raw.figures,
                "sections": azure_raw.sections,
                "metadata": azure_raw.metadata,
            }
            tracker.save_checkpoint("azure_result", azure_result)
            tracker.end_stage("azure_di",
                output_summary={
                    "pages": len(azure_result["pages"]),
                    "paragraphs": len(azure_result["paragraphs"]),
                    "tables": len(azure_result["tables"]),
                    "figures": len(azure_result["figures"]),
                })

        # ── 2. 页面分组 ─────────────────────────────
        if tracker.has_checkpoint("pages_data"):
            logger.info("[Pipeline] Stage 2/4: Page grouping → cache hit")
            tracker.start_stage("page_grouping", input_summary={"from": "cache"})
            pages_data = tracker.load_checkpoint("pages_data")
            tracker.end_stage("page_grouping",
                output_summary={"page_count": len(pages_data), "from": "cache"},
                from_cache=True)
        else:
            logger.info("[Pipeline] Stage 2/4: Page grouping...")
            tracker.start_stage("page_grouping")

            azure_wrapped = DocumentAnalysisResult(
                pages=azure_result["pages"],
                paragraphs=azure_result["paragraphs"],
                tables=azure_result["tables"],
                figures=azure_result["figures"],
                sections=azure_result.get("sections", []),
                metadata=azure_result.get("metadata", {}),
            )
            pages_data = _group_azure_results_by_page(
                azure_wrapped,
                start_page_id=start_page_id,
                end_page_id=end_page_id,
            )
            tracker.save_checkpoint("pages_data", pages_data)
            tracker.end_stage("page_grouping",
                output_summary={"page_count": len(pages_data)})

        if not pages_data:
            tracker.finish()
            middle_json = init_middle_json()
            return middle_json, build_model_output(
                DocumentAnalysisResult(), [], [],
            )

        # ── 无框线表格补检 ───────────────────────
        from mineru.backend.rag.borderless_table_detector import apply_borderless_detection
        pages_data = apply_borderless_detection(pages_data)

        # ── 图片相关性过滤 ───────────────────────
        #  在送 Dify 之前过滤无意义图片 (背景、图标、装饰元素)
        from mineru.backend.rag.image_relevance import apply_image_filter
        image_filter_result = apply_image_filter(pages_data)
        tracker.start_stage("image_filter")
        tracker.end_stage("image_filter",
            output_summary={"kept": image_filter_result.kept,
                            "skipped": image_filter_result.skipped})

        # ── 跨页表格合并 ───────────────────────────
        #  检测并合并跨页延续的表格, 移除续页的重复表头
        #  保留 rowspan/colspan 合并单元格
        from mineru.backend.rag.table_continuation import apply_cross_page_table_merge
        pages_data = apply_cross_page_table_merge(pages_data)

        # ── 可视化: Layout 框线 ─────────────────────
        #  异步生成前几页的 layout 可视化 (不阻塞主流程)
        _ = asyncio.create_task(
            _generate_layout_viz_async(tracker, pages_data[:3])
        )

        # ── 3. Dify 增强 ────────────────────────────
        if not dify_available:
            # ★ 跳过: Dify 未配置, 直接用 Azure DI 原始结果
            logger.info("[Pipeline] Stage 3/4: Dify → skipped (not configured)")
            tracker.start_stage("dify_enhancement",
                input_summary={"skipped": "Dify not configured"})
            valid_image_results: list[DifyImageResult] = []
            valid_table_results: list[DifyTableResult] = []
            tracker.end_stage("dify_enhancement",
                output_summary={"images": 0, "tables": 0, "status": "skipped"})
        elif tracker.has_checkpoint("dify_results"):
            logger.info("[Pipeline] Stage 3/4: Dify enhancement → cache hit")
            tracker.start_stage("dify_enhancement", input_summary={"from": "cache"})
            dify_cache = tracker.load_checkpoint("dify_results")
            valid_image_results = dify_cache.get("images", [])
            valid_table_results = dify_cache.get("tables", [])
            tracker.end_stage("dify_enhancement",
                output_summary={"images": len(valid_image_results),
                                "tables": len(valid_table_results), "from": "cache"},
                from_cache=True)
        else:
            logger.info("[Pipeline] Stage 3/4: Dify enhancement...")
            tracker.start_stage("dify_enhancement",
                input_summary={"total_figures": sum(len(p.get("figures", [])) for p in pages_data),
                               "total_tables": sum(len(p.get("tables", [])) for p in pages_data)})

            dify_sem = asyncio.Semaphore(DIFY_CONCURRENT_CALLS)
            dify_image_tasks, dify_table_tasks = _build_dify_tasks(
                pages_data, dify_client, dify_sem,
            )
            logger.info(f"Dify: {len(dify_image_tasks)} images, {len(dify_table_tasks)} tables")

            valid_image_results, valid_table_results = await _execute_dify_enhancement(
                dify_image_tasks, dify_table_tasks,
            )
            tracker.save_checkpoint("dify_results", {
                "images": valid_image_results,
                "tables": valid_table_results,
            })
            tracker.end_stage("dify_enhancement",
                output_summary={"images": len(valid_image_results),
                                "tables": len(valid_table_results)})

        # ── 可视化: Dify 前后对比 ────────────────────
        _ = asyncio.create_task(
            _generate_dify_viz_async(tracker, valid_image_results, valid_table_results)
        )

        # ── 4. middle_json 构建 ──────────────────────
        if tracker.has_checkpoint("middle_json"):
            logger.info("[Pipeline] Stage 4/4: middle_json → cache hit")
            tracker.start_stage("middle_json_build", input_summary={"from": "cache"})
            middle_json = tracker.load_checkpoint("middle_json")
            tracker.end_stage("middle_json_build",
                output_summary={"pages": len(middle_json.get("pdf_info", [])), "from": "cache"},
                from_cache=True)
        else:
            logger.info("[Pipeline] Stage 4/4: Building middle_json...")
            tracker.start_stage("middle_json_build")

            middle_json = init_middle_json()
            progress_bar = tqdm(total=len(pages_data), desc="Building output")

            for page_data in pages_data:
                page_num = page_data["page_number"]
                page_image_results = [
                    r for r in valid_image_results
                    if getattr(r, 'page_number', -1) == page_num
                ]
                page_table_results = [
                    r for r in valid_table_results
                    if getattr(r, 'page_number', -1) == page_num
                ]

                append_page_results_to_middle_json(
                    middle_json=middle_json,
                    page_results=[page_data],
                    dify_image_results=page_image_results,
                    dify_table_results=page_table_results,
                    page_start_index=0,
                    progress_bar=progress_bar,
                )

            progress_bar.close()
            finalize_middle_json(middle_json["pdf_info"])

            # ★ PDF 超链接回插: 从原始 PDF 提取超链接并匹配到 span
            from mineru.backend.rag.hyperlink_mapper import apply_hyperlinks_to_middle_json
            apply_hyperlinks_to_middle_json(middle_json, pdf_bytes)

            tracker.save_checkpoint("middle_json", middle_json)
            tracker.end_stage("middle_json_build",
                output_summary={"pages": len(middle_json["pdf_info"])})

        # ── 5. model_output ──────────────────────────
        tracker.start_stage("model_output")
        model_output = build_model_output(
            azure_result=DocumentAnalysisResult(
                pages=azure_result["pages"],
                paragraphs=azure_result["paragraphs"],
                tables=azure_result["tables"],
                figures=azure_result["figures"],
                metadata=azure_result.get("metadata", {}),
            ),
            dify_image_results=valid_image_results,
            dify_table_results=valid_table_results,
        )
        tracker.end_stage("model_output")

        # ── 6. 完成 ──────────────────────────────────
        tracker.finish()
        tracker.export_report()

        # 生成时间线图
        _ = asyncio.create_task(_generate_timeline_async(tracker))

        logger.info(
            f"[Pipeline] Complete: {tracker.export_stage_summary()}"
        )

        return middle_json, model_output

    except Exception as e:
        tracker.finish(error=e)
        tracker.export_report()
        logger.exception(f"[Pipeline] Failed: {e}")
        raise


# ── 流式多文档分析 (兼容 pipeline 接口) ─────────────────

# ── 链式 API (新) ─────────────────────────────────────────

async def aio_doc_analyze_chain(
    pdf_bytes: bytes,
    output_dir: str = ".",
    doc_stem: str = "document",
    chain: Optional["PipelineChain"] = None,
    **params,
) -> tuple[dict, dict]:
    """
    基于 PipelineChain 的异步文档分析入口。

    支持三种调用方式 (按优先级):
    1. 传入自定义 chain (完全自定义处理流程)
    2. 通过 params["chain_config"] 指定 JSON 配置文件路径
    3. 通过 params["chain_names"] 指定阶段名称列表
    4. 默认使用 default_rag_chain()

    Examples:
        # 默认链
        result = await aio_doc_analyze_chain(pdf_bytes, output_dir="./out")

        # 最小链 (跳过 Dify)
        from mineru.backend.rag.pipeline.chain import minimal_rag_chain
        result = await aio_doc_analyze_chain(pdf_bytes, chain=minimal_rag_chain())

        # 从配置文件
        result = await aio_doc_analyze_chain(
            pdf_bytes, chain_config="pipelines/my_flow.json"
        )

        # 从名称列表
        result = await aio_doc_analyze_chain(
            pdf_bytes, chain_names=["pdf_load", "azure_di", "page_group",
                                     "table_merge", "build_middle_json"]
        )

        # 动态修改链
        from mineru.backend.rag.pipeline.chain import default_rag_chain
        chain = default_rag_chain().disable("image_filter").disable("dify_enhance")
        result = await aio_doc_analyze_chain(pdf_bytes, chain=chain)
    """
    from mineru.backend.rag.pipeline.chain import PipelineChain, default_rag_chain
    from mineru.backend.rag.pipeline.context import PipelineContext

    # 解析 chain
    if chain is None:
        config_path = params.pop("chain_config", None)
        names = params.pop("chain_names", None)
        if config_path:
            chain = PipelineChain.from_config(config_path)
        elif names:
            chain = PipelineChain.from_names(names)
        else:
            chain = default_rag_chain()

    # 构建上下文
    ctx = PipelineContext(
        pdf_bytes=pdf_bytes,
        output_dir=output_dir,
        doc_stem=doc_stem,
        params={
            "start_page_id": params.pop("start_page_id", 0),
            "end_page_id": params.pop("end_page_id", None),
            "lang": params.pop("lang", ""),
            "parse_method": params.pop("parse_method", "auto"),
            "formula_enable": params.pop("formula_enable", True),
            "table_enable": params.pop("table_enable", True),
            **params,
        },
    )

    # 执行链
    ctx = await chain.run(ctx)

    if not ctx.is_healthy:
        logger.warning(f"Pipeline completed with errors: {ctx.errors}")

    return ctx.middle_json or init_middle_json(), ctx.model_output or {}


def doc_analyze_chain(
    pdf_bytes: bytes,
    output_dir: str = ".",
    doc_stem: str = "document",
    chain=None,
    **params,
) -> tuple[dict, dict]:
    """同步版链式文档分析入口"""
    return asyncio.run(
        aio_doc_analyze_chain(
            pdf_bytes=pdf_bytes, output_dir=output_dir,
            doc_stem=doc_stem, chain=chain, **params,
        )
    )


# ── 流式多文档分析 ─────────────────────────────────────

# ── Excel 入口 ───────────────────────────────────────────

async def parse_excel(
    file_path: str,
    output_dir: str,
    use_dify: bool = True,
) -> dict:
    """
    Excel 文件处理入口。

    使用链式 API, 自动选择 excel_chain。
    """
    from pathlib import Path
    from mineru.backend.rag.pipeline.chain import excel_chain

    path = Path(file_path)
    pdf_bytes = path.read_bytes()
    chain = excel_chain()

    ctx = await aio_doc_analyze_chain(
        pdf_bytes=pdf_bytes,
        output_dir=output_dir,
        doc_stem=path.stem,
        chain=chain if use_dify else PipelineChain.from_names(["excel_process"]),
    )

    # 提取 Markdown
    from mineru.backend.rag.rag_middle_json_mkcontent import union_make
    from mineru.utils.enum_class import MakeMode

    md = union_make(ctx.middle_json["pdf_info"], MakeMode.MM_MD, "")
    output_path = Path(output_dir) / f"{path.stem}.md"
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(md, encoding="utf-8")

    return {"output_path": str(output_path), "result": ctx.metadata.get("excel_result")}


def doc_analyze_streaming(
    pdf_bytes_list: list[bytes],
    image_writer_list: list[FileBasedDataWriter],
    lang_list: list[str],
    on_doc_ready,
    parse_method: str = "auto",
    formula_enable: bool = True,
    table_enable: bool = True,
    **kwargs,
) -> None:
    """流式多文档批量分析"""
    if not (len(pdf_bytes_list) == len(image_writer_list) == len(lang_list)):
        raise ValueError("pdf_bytes_list, image_writer_list, lang_list 长度必须一致")

    for doc_index, (pdf_bytes, image_writer, lang) in enumerate(
        zip(pdf_bytes_list, image_writer_list, lang_list)
    ):
        try:
            middle_json, model_output = doc_analyze(
                pdf_bytes=pdf_bytes, image_writer=image_writer,
                output_dir=str(image_writer._parent_dir) if hasattr(image_writer, '_parent_dir') else None,
                doc_stem=f"doc_{doc_index}", lang=lang,
                parse_method=parse_method, formula_enable=formula_enable,
                table_enable=table_enable, **kwargs,
            )
            on_doc_ready(doc_index, [model_output], middle_json, ocr_enable=False)
        except Exception as e:
            logger.exception(f"doc {doc_index} failed: {e}")
            on_doc_ready(doc_index, [{"error": str(e)}], init_middle_json(), ocr_enable=False)


# ── 便捷函数 ────────────────────────────────────────────

async def parse_document(
    file_path: str, output_dir: str,
    lang: str = "",
    formula_enable: bool = True, table_enable: bool = True,
    start_page_id: int = 0, end_page_id: Optional[int] = None,
) -> dict:
    """一站式文档解析: 文件路径 → 输出目录"""
    import json, os
    from pathlib import Path
    from mineru.cli.common import read_fn, prepare_env
    from mineru.backend.rag.rag_middle_json_mkcontent import union_make
    from mineru.utils.enum_class import MakeMode

    path = Path(file_path)
    pdf_bytes = read_fn(path)
    pdf_file_name = path.stem
    local_image_dir, local_md_dir = prepare_env(output_dir, pdf_file_name, "rag")
    md_writer = FileBasedDataWriter(local_md_dir)

    middle_json, model_output = await aio_doc_analyze(
        pdf_bytes=pdf_bytes,
        image_writer=FileBasedDataWriter(local_image_dir),
        output_dir=str(output_dir), doc_stem=pdf_file_name,
        lang=lang, formula_enable=formula_enable, table_enable=table_enable,
        start_page_id=start_page_id, end_page_id=end_page_id,
    )

    img_dir = os.path.basename(local_image_dir)
    pdf_info = middle_json["pdf_info"]

    # 输出生成: CPU 密集且受 GIL 限制, 串行即可
    # union_make 三次调用共享 pdf_info 引用, 无额外内存开销
    md_content = union_make(pdf_info, MakeMode.MM_MD, img_dir)
    md_writer.write_string(f"{pdf_file_name}.md", md_content)

    content_list = union_make(pdf_info, MakeMode.CONTENT_LIST, img_dir)
    md_writer.write_string(
        f"{pdf_file_name}_content_list.json",
        json.dumps(content_list, ensure_ascii=False, indent=4),
    )

    content_list_v2 = union_make(pdf_info, MakeMode.CONTENT_LIST_V2, img_dir)
    md_writer.write_string(
        f"{pdf_file_name}_content_list_v2.json",
        json.dumps(content_list_v2, ensure_ascii=False, indent=4),
    )

    md_writer.write_string(
        f"{pdf_file_name}_middle.json",
        json.dumps(middle_json, ensure_ascii=False, indent=4),
    )
    md_writer.write_string(
        f"{pdf_file_name}_model.json",
        json.dumps(model_output, ensure_ascii=False, indent=4),
    )

    logger.info(f"Output: {local_md_dir}")
    return {"output_dir": local_md_dir, "image_dir": local_image_dir,
            "middle_json": middle_json, "model_output": model_output}


if __name__ == "__main__":
    import sys

    async def _main():
        if len(sys.argv) < 3:
            print("Usage: python -m mineru.backend.rag.rag_analyze <input_pdf> <output_dir>")
            sys.exit(1)
        result = await parse_document(sys.argv[1], sys.argv[2])
        print(f"Done: {result['output_dir']}")

    asyncio.run(_main())
