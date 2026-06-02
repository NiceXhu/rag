# Copyright (c) Opendatalab. All rights reserved.
"""
Azure DI 分析阶段 — 发送 PDF 到 Azure Document Intelligence。
"""
from mineru.backend.rag.pipeline.stage import PipelineStage, StageConfig
from mineru.backend.rag.pipeline.context import PipelineContext
from mineru.backend.rag.azure_doc_intelligence import AzureDocumentIntelligenceClient


class AzureDIAnalyzeStage(PipelineStage):
    name = "azure_di"

    async def execute(self, ctx: PipelineContext) -> PipelineContext:
        client = AzureDocumentIntelligenceClient()
        pdf = ctx.effective_pdf_bytes or ctx.pdf_bytes

        azure_raw = await client.analyze_document(
            file_bytes=pdf,
            content_type="application/pdf",
        )

        ctx.azure_result = {
            "pages": azure_raw.pages,
            "paragraphs": azure_raw.paragraphs,
            "tables": azure_raw.tables,
            "figures": azure_raw.figures,
            "sections": azure_raw.sections,
            "metadata": azure_raw.metadata,
        }
        return ctx

    def _build_output_summary(self, ctx: PipelineContext) -> dict:
        r = ctx.azure_result or {}
        return {
            "pages": len(r.get("pages", [])),
            "paragraphs": len(r.get("paragraphs", [])),
            "tables": len(r.get("tables", [])),
            "figures": len(r.get("figures", [])),
        }

    def _restore_from_cache(self, ctx: PipelineContext, cached: dict) -> None:
        ctx.azure_result = cached
