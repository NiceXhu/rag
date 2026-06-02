# Copyright (c) Opendatalab. All rights reserved.
"""
model_output 构建阶段。
"""
from mineru.backend.rag.pipeline.stage import PipelineStage, StageConfig
from mineru.backend.rag.pipeline.context import PipelineContext
from mineru.backend.rag.azure_doc_intelligence import DocumentAnalysisResult
from mineru.backend.rag.model_output_to_middle_json import build_model_output


class ModelOutputStage(PipelineStage):
    name = "model_output"

    async def execute(self, ctx: PipelineContext) -> PipelineContext:
        if not ctx.azure_result:
            raise ValueError("azure_result is empty")

        ctx.model_output = build_model_output(
            azure_result=DocumentAnalysisResult(
                pages=ctx.azure_result["pages"],
                paragraphs=ctx.azure_result["paragraphs"],
                tables=ctx.azure_result["tables"],
                figures=ctx.azure_result["figures"],
                metadata=ctx.azure_result.get("metadata", {}),
            ),
            dify_image_results=ctx.dify_image_results,
            dify_table_results=ctx.dify_table_results,
        )
        return ctx

    def _build_output_summary(self, ctx: PipelineContext) -> dict:
        return {"status": "completed"}
