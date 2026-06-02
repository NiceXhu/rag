# Copyright (c) Opendatalab. All rights reserved.
"""
页面分组阶段 — 将 Azure DI 结果按页码重组。
"""
from mineru.backend.rag.pipeline.stage import PipelineStage, StageConfig
from mineru.backend.rag.pipeline.context import PipelineContext
from mineru.backend.rag.azure_doc_intelligence import DocumentAnalysisResult
from mineru.backend.rag.rag_analyze import _group_azure_results_by_page


class PageGroupStage(PipelineStage):
    name = "page_group"

    async def execute(self, ctx: PipelineContext) -> PipelineContext:
        if not ctx.azure_result:
            raise ValueError("azure_result is empty — 确保 azure_di 阶段先执行")

        azure_wrapped = DocumentAnalysisResult(
            pages=ctx.azure_result["pages"],
            paragraphs=ctx.azure_result["paragraphs"],
            tables=ctx.azure_result["tables"],
            figures=ctx.azure_result["figures"],
            sections=ctx.azure_result.get("sections", []),
            metadata=ctx.azure_result.get("metadata", {}),
        )

        ctx.pages_data = _group_azure_results_by_page(
            azure_wrapped,
            start_page_id=ctx.params.get("start_page_id", 0),
            end_page_id=ctx.params.get("end_page_id", None),
        )
        return ctx

    def _build_output_summary(self, ctx: PipelineContext) -> dict:
        return {"page_count": len(ctx.pages_data or [])}

    def _restore_from_cache(self, ctx: PipelineContext, cached: list) -> None:
        ctx.pages_data = cached
