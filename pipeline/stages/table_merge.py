# Copyright (c) Opendatalab. All rights reserved.
"""
跨页表格处理阶段 — 表头补全 + 跨页合并 (含合并单元格保留)。

默认模式: "complete" — 保持分页, 为缺失表头的续页补全表头
可选模式: "merge" — 合并所有分片为单个逻辑表格 (通过 params.mode 配置)
"""
from mineru.backend.rag.pipeline.stage import PipelineStage, StageConfig
from mineru.backend.rag.pipeline.context import PipelineContext
from mineru.backend.rag.table_continuation import (
    apply_cross_page_table_merge,
    complete_table_headers,
)


class TableMergeStage(PipelineStage):
    name = "table_merge"

    async def execute(self, ctx: PipelineContext) -> PipelineContext:
        if not ctx.pages_data:
            raise ValueError("pages_data is empty")

        mode = self.config.params.get("mode", "complete")

        if mode == "merge":
            # 合并模式: 所有分片合并为一个逻辑表格
            ctx.pages_data = apply_cross_page_table_merge(ctx.pages_data)
        else:
            # 补全模式 (默认): 保持分页, 补全缺失表头
            ctx.pages_data = complete_table_headers(ctx.pages_data)

        return ctx

    def _build_output_summary(self, ctx: PipelineContext) -> dict:
        tables = sum(len(p.get("tables", [])) for p in (ctx.pages_data or []))
        mode = self.config.params.get("mode", "complete")
        return {"total_tables": tables, "mode": mode}
