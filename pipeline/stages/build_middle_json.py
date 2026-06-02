# Copyright (c) Opendatalab. All rights reserved.
"""
middle_json 构建阶段 — 将处理结果序列化为标准 middle_json。
"""
from tqdm import tqdm

from mineru.backend.rag.pipeline.stage import PipelineStage, StageConfig
from mineru.backend.rag.pipeline.context import PipelineContext
from mineru.backend.rag.model_output_to_middle_json import (
    init_middle_json,
    append_page_results_to_middle_json,
    finalize_middle_json,
)


class BuildMiddleJsonStage(PipelineStage):
    name = "build_middle_json"

    async def execute(self, ctx: PipelineContext) -> PipelineContext:
        if not ctx.pages_data:
            raise ValueError("pages_data is empty")

        ctx.middle_json = init_middle_json()
        progress_bar = tqdm(total=len(ctx.pages_data), desc="Building output")

        for page_data in ctx.pages_data:
            page_num = page_data["page_number"]
            append_page_results_to_middle_json(
                middle_json=ctx.middle_json,
                page_results=[page_data],
                dify_image_results=[
                    r for r in ctx.dify_image_results
                    if getattr(r, 'page_number', -1) == page_num
                ],
                dify_table_results=[
                    r for r in ctx.dify_table_results
                    if getattr(r, 'page_number', -1) == page_num
                ],
                page_start_index=0,
                progress_bar=progress_bar,
            )

        progress_bar.close()
        finalize_middle_json(ctx.middle_json["pdf_info"])
        return ctx

    def _build_output_summary(self, ctx: PipelineContext) -> dict:
        return {"pages": len((ctx.middle_json or {}).get("pdf_info", []))}

    def _restore_from_cache(self, ctx: PipelineContext, cached: dict) -> None:
        ctx.middle_json = cached
