# Copyright (c) Opendatalab. All rights reserved.
"""
Dify 增强阶段 — 图片描述 + 表格优化。
"""
import asyncio

from mineru.backend.rag.pipeline.stage import PipelineStage, StageConfig
from mineru.backend.rag.pipeline.context import PipelineContext
from mineru.backend.rag.dify_client import DifyWorkflowClient
from mineru.backend.rag.rag_analyze import (
    _build_dify_tasks,
    _execute_dify_enhancement,
    DIFY_CONCURRENT_CALLS,
)


class DifyEnhanceStage(PipelineStage):
    name = "dify_enhance"

    async def execute(self, ctx: PipelineContext) -> PipelineContext:
        if not ctx.pages_data:
            raise ValueError("pages_data is empty")

        dify_client = DifyWorkflowClient()

        # 如果 Dify 未配置, 跳过
        if not dify_client.is_configured:
            ctx.dify_image_results = []
            ctx.dify_table_results = []
            ctx.metadata["dify_skipped"] = True
            return ctx

        sem = asyncio.Semaphore(DIFY_CONCURRENT_CALLS)
        img_tasks, tbl_tasks = _build_dify_tasks(ctx.pages_data, dify_client, sem)

        ctx.dify_image_results, ctx.dify_table_results = (
            await _execute_dify_enhancement(img_tasks, tbl_tasks)
        )
        return ctx

    def _build_output_summary(self, ctx: PipelineContext) -> dict:
        if ctx.metadata.get("dify_skipped"):
            return {"status": "skipped (not configured)"}
        return {
            "images": len(ctx.dify_image_results),
            "tables": len(ctx.dify_table_results),
        }

    def _restore_from_cache(self, ctx: PipelineContext, cached: dict) -> None:
        ctx.dify_image_results = cached.get("images", [])
        ctx.dify_table_results = cached.get("tables", [])
