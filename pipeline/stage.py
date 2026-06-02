# Copyright (c) Opendatalab. All rights reserved.
"""
Pipeline 阶段基类。

每个处理阶段继承 PipelineStage, 实现 execute() 方法。
阶段之间通过 PipelineContext 传递状态, 无需直接耦合。
"""
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Optional

from loguru import logger

from mineru.backend.rag.pipeline.context import PipelineContext


@dataclass
class StageConfig:
    """阶段配置"""
    enabled: bool = True                # 是否启用
    checkpoint: bool = True             # 是否支持缓存恢复
    required: bool = True               # 是否必须成功 (失败则中止 Pipeline)
    timeout_s: float = 0                # 超时秒数 (0 = 无超时)
    params: dict = field(default_factory=dict)  # 阶段特定参数


class PipelineStage(ABC):
    """
    Pipeline 阶段抽象基类。

    每个阶段:
    - 有唯一的 name (用于日志、缓存 key、配置引用)
    - 通过 execute(ctx) 处理上下文
    - 可以通过 can_skip(ctx) 决定是否跳过
    - 通过 config 控制行为 (enabled/required/checkpoint/params)

    子类只需要实现 execute() 和 name 属性。
    """

    name: str = "__base__"
    config: StageConfig = field(default_factory=StageConfig)

    def __init_subclass__(cls, **kwargs):
        """自动注入 StageConfig 默认值"""
        super().__init_subclass__(**kwargs)

    def __init__(self, config: Optional[StageConfig] = None):
        self.config = config or StageConfig()

    @abstractmethod
    async def execute(self, ctx: PipelineContext) -> PipelineContext:
        """
        执行本阶段的处理逻辑。

        Args:
            ctx: Pipeline 共享上下文 (可读写)

        Returns:
            修改后的上下文

        Raises:
            Exception: 如果 required=True, 异常会中止 Pipeline
        """
        ...

    async def run(self, ctx: PipelineContext) -> PipelineContext:
        """
        标准执行入口 (带日志/追踪/异常处理/缓存检查)。

        子类通常不需要重写此方法, 只需实现 execute()。
        """
        if not self.config.enabled:
            logger.info(f"[{self.name}] 已禁用, 跳过")
            ctx.stage_results[self.name] = {"status": "disabled"}
            return ctx

        # 检查点缓存
        if self.config.checkpoint and ctx.tracker is not None:
            if ctx.tracker.has_checkpoint(self.name):
                logger.info(f"[{self.name}] 缓存命中, 恢复中...")
                ctx.tracker.start_stage(self.name, {"from": "cache"})
                try:
                    cached = ctx.tracker.load_checkpoint(self.name)
                    self._restore_from_cache(ctx, cached)
                    ctx.tracker.end_stage(
                        self.name,
                        output_summary={"from": "cache"},
                        from_cache=True,
                    )
                    return ctx
                except Exception as e:
                    logger.warning(f"[{self.name}] 缓存恢复失败: {e}, 重新执行")

        # 执行
        logger.info(f"[{self.name}] 开始执行...")
        ctx.tracker and ctx.tracker.start_stage(self.name, self.config.params)

        try:
            ctx = await self.execute(ctx)
            ctx.tracker and ctx.tracker.end_stage(
                self.name,
                output_summary=self._build_output_summary(ctx),
            )
            logger.info(f"[{self.name}] 完成")
        except Exception as e:
            ctx.add_error(self.name, str(e), fatal=self.config.required)
            ctx.tracker and ctx.tracker.end_stage(self.name, error=e)
            logger.error(f"[{self.name}] 失败: {e}")
            if self.config.required:
                raise
            logger.warning(f"[{self.name}] 非关键阶段, 继续执行")

        return ctx

    def _build_output_summary(self, ctx: PipelineContext) -> dict:
        """构建输出摘要 (子类可重写)"""
        return {"status": "completed"}

    def _restore_from_cache(self, ctx: PipelineContext, cached: Any) -> None:
        """
        从缓存恢复上下文状态 (子类必须重写)。

        默认不恢复任何内容, 子类需要根据自身的产出物恢复 ctx 字段。
        """
        pass

    def _save_to_cache(self, ctx: PipelineContext, data: Any) -> None:
        """保存中间结果到缓存"""
        if self.config.checkpoint and ctx.tracker is not None:
            try:
                ctx.tracker.save_checkpoint(self.name, data)
            except Exception as e:
                logger.warning(f"[{self.name}] 缓存保存失败: {e}")

    def can_skip(self, ctx: PipelineContext) -> bool:
        """判断是否可以跳过本阶段"""
        return False

    def __repr__(self):
        return f"<{self.name} enabled={self.config.enabled}>"
