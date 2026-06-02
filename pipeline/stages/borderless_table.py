# Copyright (c) Opendatalab. All rights reserved.
"""
无框线表格检测阶段。
"""
from mineru.backend.rag.pipeline.stage import PipelineStage, StageConfig
from mineru.backend.rag.pipeline.context import PipelineContext
from mineru.backend.rag.borderless_table_detector import apply_borderless_detection


class BorderlessTableStage(PipelineStage):
    name = "borderless_table"

    async def execute(self, ctx: PipelineContext) -> PipelineContext:
        if not ctx.pages_data:
            raise ValueError("pages_data is empty")
        ctx.pages_data = apply_borderless_detection(ctx.pages_data)
        return ctx

    def _build_output_summary(self, ctx: PipelineContext) -> dict:
        tables = sum(len(p.get("tables", [])) for p in (ctx.pages_data or []))
        return {"total_tables_after": tables}
