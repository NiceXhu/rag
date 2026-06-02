# MinerU 原生 vs RAG 增强 — 全方位对比

> 对比 MinerU 内置 Pipeline/VLM/Hybrid 后端 与 新 RAG 后端 (Azure DI + Dify) 的差异

---

## 目录

1. [架构对比](#1-架构对比)
2. [处理能力对比](#2-处理能力对比)
3. [性能与资源](#3-性能与资源)
4. [输出质量](#4-输出质量)
5. [可观测与调试](#5-可观测与调试)
6. [扩展性](#6-扩展性)
7. [部署与成本](#7-部署与成本)
8. [选型建议](#8-选型建议)

---

## 1. 架构对比

### 1.1 整体架构

```
┌─── MinerU 原生 ───────────────┐    ┌─── RAG 后端 ────────────────────┐
│                                │    │                                  │
│  输入 (PDF/图片/Office)        │    │  输入 (PDF/图片/Excel/Office)    │
│         │                      │    │         │                        │
│         ▼                      │    │         ▼                        │
│  ┌──────────────┐              │    │  ┌────────────────────┐          │
│  │ do_parse()   │              │    │  │ dispatch_by_type() │ ← 自动路由│
│  │ 硬编码分发    │              │    │  │ 类型检测 + 多链路   │          │
│  └──┬───┬───┬───┘              │    │  └──┬───┬───┬─────────┘          │
│     │   │   │                  │    │     │   │   │                    │
│  ┌──┘   │   └──┐               │    │  ┌──┘   │   └──┐                 │
│  ▼      ▼      ▼               │    │  ▼      ▼      ▼                 │
│ Pipeline VLM  Hybrid            │    │ RAG   Excel  Office              │
│ (本地)  (本地) (本地+VLM)        │    │ Chain  Chain  Backend            │
│                                  │    │                                 │
│  后端选择: CLI --backend 参数     │    │  后端选择: 自动检测文件后缀      │
│  需用户显式指定                   │    │  无需用户干预                   │
└──────────────────────────────────┘    └─────────────────────────────────┘
```

### 1.2 处理链路

| 维度 | MinerU 原生 | RAG 后端 |
|------|-----------|---------|
| **PDF 布局分析** | PP-DocLayoutV2 (本地模型) | Azure DI prebuilt-layout (云端) |
| **OCR** | PaddleOCR PyTorch (本地) | Azure DI 内置 OCR (109 语言) |
| **公式识别** | UniMERNet / PP-FormulaNet (本地) | Azure DI 基础公式检测 |
| **表格识别** | SLANet+ / UNet (本地) | Azure DI 结构化表格 (含 rowspan/colspan) |
| **图片增强** | 无 | Dify Workflow + LLM 描述生成 |
| **表格优化** | 无 | Dify Workflow + LLM 结构优化 |
| **超链接保留** | 无 | PDF annotation 提取 → Markdown 回插 |
| **装饰图片过滤** | 无 | 九级过滤漏斗 |
| **无框线表格** | 布局模型检测 | 布局模型 + 启发式补检 |
| **跨页表格合并** | 基于 bbox 合并 | Azure DI kind 标注 + 签名匹配 + bbox |
| **Excel 处理** | openpyxl (基础) | 专用 Excel 链路 + Dify 优化 + 大文件流式 |
| **PPTX 处理** | pypptx 原生 | 委托 Office 原生后端 |

### 1.3 并发模型

| | MinerU 原生 | RAG 后端 |
|------|-----------|---------|
| **Azure/Cloud** | — | 单次调用, 服务端内部并行 |
| **本地模型推理** | GPU batch inference, sliding window | — |
| **Dify 调用** | — | asyncio.gather + Semaphore(8) |
| **Dify 连接** | — | httpx 共享连接池 (keep-alive) |
| **图片+表格** | — | 混合竞争并发槽 |
| **PDF 渲染** | ProcessPoolExecutor(3) | 无 (PDF 直送, 不渲染) |

---

## 2. 处理能力对比

### 2.1 输入格式

| 格式 | MinerU 原生 | RAG 后端 |
|------|-----------|---------|
| PDF | ✅ 全后端支持 | ✅ 默认链 (10 阶段) |
| PNG/JPG/WebP/... | ✅ Pipeline/Hybrid | ✅ 转 PDF 后送 Azure DI |
| DOCX | ✅ Office 后端 | ✅ 委托 Office 后端 |
| PPTX | ✅ Office 后端 | ✅ 委托 Office 后端 |
| **XLSX/XLS/CSV** | ⚠️ openpyxl 基础转换 | ✅ **专用处理器 + Dify** |
| 扫描件 PDF | ✅ OCR 自动检测 | ✅ Azure DI 内置 OCR |

### 2.2 语言支持

| | MinerU 原生 | RAG 后端 |
|------|-----------|---------|
| OCR 语言数 | 109 种 (PaddleOCR) | 109+ 种 (Azure DI) |
| 语言切换 | CLI `--lang` 参数 | `lang` 参数透传 Azure DI |
| 自动检测 | 需手动指定 | Azure DI 自动语言检测 |

### 2.3 最大文件规模

| | MinerU 原生 | RAG 后端 |
|------|-----------|---------|
| PDF 页数 | 滑动窗口, 理论无限 | Azure DI 单次 ≤ 2000 页 |
| 超大 PDF (>2000 页) | 自动分片 | 需手动拆分 |
| Excel 行数 | 受内存限制 | read_only 流式, 理论无限 |
| Excel Dify 上限 | — | >5000 行自动跳过 (可配置) |

### 2.4 表格处理深度

| 能力 | MinerU 原生 | RAG 后端 |
|------|-----------|---------|
| 表格结构检测 | ✅ SLANet+/UNet | ✅ Azure DI |
| **合并单元格 (rowspan/colspan)** | ⚠️ 部分支持 | ✅ Azure DI 原生检测 |
| **跨页表格合并** | ✅ bbox 合并 | ✅ bbox + 签名 + kind 标注 |
| **表头去重** | 基于位置 | ✅ Azure DI `kind:columnHeader` |
| **无框线表格** | 布局模型检测 | ✅ 布局 + 启发式 X/Y 聚类 |
| **表格内容优化** | ❌ | ✅ Dify LLM 优化 |
| **Excel Sheet 处理** | ⚠️ 基础转换 | ✅ 全 Sheet + 合并单元格 + Dify |

### 2.5 图片处理

| 能力 | MinerU 原生 | RAG 后端 |
|------|-----------|---------|
| 图片提取 | ✅ 裁剪保存 | ✅ Azure DI figure 检测 |
| **图片描述生成** | ❌ | ✅ Dify LLM (含图表/照片分类) |
| **装饰图片过滤** | ❌ | ✅ 9 级漏斗 (尺寸/位置/重复/内容/重叠/覆盖率/嵌入/孤立) |
| **背景图片检测** | ❌ | ✅ 文本重叠 + 高覆盖率 |
| **无关截图过滤** | ❌ | ✅ 7 类内容检测器 (可扩展) |
| 图表 vs 图片分类 | ⚠️ 依赖布局模型 | ✅ Azure DI + Dify 确认 |

### 2.6 超链接

| | MinerU 原生 | RAG 后端 |
|------|-----------|---------|
| PDF 超链接提取 | ❌ | ✅ pypdfium2 annotation → bbox 匹配 |
| Markdown 回插 | ❌ | ✅ `[text](url)` 精确位置匹配 |
| 表格内链接 | ❌ | ✅ 去重 + 合并 |

---

## 3. 性能与资源

### 3.1 硬件需求

| | MinerU 原生 (Pipeline) | MinerU 原生 (VLM) | RAG 后端 |
|------|----------------------|-------------------|---------|
| **CPU 模式** | ✅ 支持 | ❌ 需要 GPU | ✅ 支持 |
| **GPU 需求** | 可选 (加速) | **必需** (≥8GB VRAM) | **不需要** |
| **内存** | 8GB+ (含模型) | 16GB+ (含 VLM) | 512MB+ |
| **磁盘** | 模型 2-5GB | 模型 5-10GB | 无模型下载 |
| **网络** | 仅模型下载 | 仅模型下载 | **需要稳定网络** |

### 3.2 处理速度 (200 页 PDF, 含 30 图片 + 12 表格)

| 阶段 | MinerU 原生 (Pipeline, GPU) | RAG 后端 |
|------|---------------------------|---------|
| 布局分析/OCR | ~15s (本地推理) | — |
| Azure DI | — | ~10s (网络 + 云端处理) |
| 公式识别 | ~5s | — |
| 表格识别 | ~3s | — |
| 图片增强 | — | ~6s (Dify × 30) |
| 表格优化 | — | ~3s (Dify × 12) |
| 内容过滤 | — | ~0.5s |
| 输出生成 | ~2s | ~1s |
| **总计** | **~25s** | **~20s** |

> 注: VLM 后端 (本地 GPU) 耗时通常 30-60s, Hybrid 后端 40-80s
> RAG 后端的 Dify 耗时与并发数成反比 (Semaphore 8 → 理论 8x 加速)

### 3.3 成本估算 (单文档 200 页)

| | MinerU 原生 | RAG 后端 |
|------|-----------|---------|
| 计算成本 | GPU/CPU 电费 | $0 |
| **Azure DI** | $0 | ~$2.00 (200 页 × $0.01/page) |
| **Dify (LLM)** | $0 | ~$0.30 (42 次调用) |
| **总 API 成本** | **$0** | **~$2.30** |
| 图片过滤节省 | — | 可节省 60-87% Dify 调用 |

---

## 4. 输出质量

### 4.1 输出格式

| 格式 | MinerU 原生 | RAG 后端 |
|------|-----------|---------|
| Markdown (MM_MD) | ✅ | ✅ |
| NLP Markdown | ✅ | ✅ |
| Content List JSON | ✅ | ✅ |
| Content List V2 | ✅ | ✅ |
| Middle JSON | ✅ | ✅ (兼容格式) |
| Model JSON | ✅ | ✅ |
| Layout 可视化 PDF | ✅ | ✅ (PNG) |
| Span 可视化 PDF | ✅ (Pipeline) | — |

### 4.2 内容质量

| 维度 | MinerU 原生 | RAG 后端 |
|------|-----------|---------|
| **文本准确性** | ⭐⭐⭐⭐⭐ 本地 OCR, 高精度 | ⭐⭐⭐⭐ Azure DI OCR |
| **表格结构** | ⭐⭐⭐⭐ 结构识别好, 无优化 | ⭐⭐⭐⭐⭐ 结构 + LLM 语义优化 |
| **表格合并单元格** | ⭐⭐⭐ 部分支持 | ⭐⭐⭐⭐⭐ Azure DI 原生 |
| **图片描述** | ❌ 无 | ⭐⭐⭐⭐ Dify LLM 生成 |
| **公式** | ⭐⭐⭐⭐⭐ 专用模型 | ⭐⭐⭐ Azure DI 基础 |
| **阅读顺序** | ⭐⭐⭐⭐ 布局模型 | ⭐⭐⭐⭐ Azure DI |
| **超链接** | ❌ 丢失 | ⭐⭐⭐⭐⭐ 精确回插 |
| **噪声过滤** | ⭐⭐⭐ 页眉/页脚 | ⭐⭐⭐⭐⭐ 9 级过滤 + 页眉/页脚 |
| **Excel 输出** | ⭐⭐ 基础 | ⭐⭐⭐⭐⭐ 全 Sheet + Dify 优化 |

---

## 5. 可观测与调试

| 能力 | MinerU 原生 | RAG 后端 |
|------|-----------|---------|
| **阶段追踪** | ❌ 仅日志 | ✅ PipelineTracker (每阶段耗时/状态) |
| **运行报告** | ❌ | ✅ `pipeline_run.json` |
| **中间结果缓存** | ❌ | ✅ 每阶段 checkpoint, 失败恢复 |
| **可视化看板** | ❌ | ✅ Web Dashboard (运行列表/甘特图/画廊) |
| **Layout 框线可视化** | ✅ PDF | ✅ PNG |
| **Dify 对比** | — | ✅ 前后对比文本 |
| **时间线图** | ❌ | ✅ 自动生成 |
| **缓存命中率** | — | ✅ 可量化 |

---

## 6. 扩展性

### 6.1 添加处理阶段

| | MinerU 原生 | RAG 后端 |
|------|-----------|---------|
| **方式** | 修改 `do_parse()` 分支 | PipelineStage 子类 + 注册 |
| **配置化** | ❌ 硬编码 | ✅ JSON 配置文件 + 名称列表 |
| **阶段开关** | 参数控制 | `chain.enable()/disable()` |
| **阶段顺序** | 固定 | 可任意编排 (insert_before/after) |

```python
# RAG 后端 — 5 种构建方式
chain = default_rag_chain()                        # 默认
chain = PipelineChain.from_names([...])            # 名称列表
chain = PipelineChain.from_config("flow.json")     # JSON 配置
chain.disable("dify_enhance")                      # 动态修改
chain.insert_after("azure_di", MyStage())          # 插入自定义
```

### 6.2 图片过滤扩展

| | MinerU 原生 | RAG 后端 |
|------|-----------|---------|
| 无关内容过滤 | ❌ | ✅ 7 类内置检测器 |
| 自定义检测器 | ❌ | ✅ `~/mineru.json` + 环境变量 + 代码注册 |
| 检测器热更新 | ❌ | ✅ `reload_detectors()` 每次 Pipeline 启动 |

### 6.3 OCR 引擎替换

| | MinerU 原生 | RAG 后端 |
|------|-----------|---------|
| 替换 OCR | 需重写 pipeline_analyze.py | 替换 `azure_di` 阶段即可 |
| 多云支持 | ❌ | ✅ 腾讯云/阿里云 (实现 PipelineStage 接口) |

---

## 7. 部署与成本

### 7.1 部署复杂度

| | MinerU 原生 | RAG 后端 |
|------|-----------|---------|
| **安装** | `pip install mineru[core]` + 模型下载 (2-10GB) | `pip install azure-ai-documentintelligence httpx` |
| **配置** | `~/mineru.json` | 3-5 个环境变量 |
| **首次启动** | 5-20 分钟 (模型下载) | < 1 分钟 |
| **离线运行** | ✅ 完全离线 | ❌ 需要网络 |
| **Docker 镜像** | 5-15GB | 500MB |
| **GPU 依赖** | VLM/Hybrid 必需 | 无 |

### 7.2 适用场景

| 场景 | 推荐方案 |
|------|---------|
| **高精度公式论文** | MinerU 原生 (Pipeline/VLM) |
| **大批量文档, 低资源** | MinerU 原生 (Pipeline, CPU) |
| **PPT 转 PDF, 大量图片** | **RAG 后端** (图片过滤 + Dify 增强) |
| **Excel 表格数据** | **RAG 后端** (专用 Excel 链路) |
| **需要图片描述/表格优化** | **RAG 后端** |
| **混合格式批量处理** | **RAG 后端** (自动路由) |
| **完全离线/数据敏感** | MinerU 原生 |
| **快速原型/低运维** | **RAG 后端** (无需 GPU, 无需模型) |
| **最高精度, 无视成本** | MinerU 原生 (Hybrid) + RAG 后端 (Dify 增强) |

---

## 8. 选型建议

### 8.1 决策树

```
                    开始
                     │
              需要处理 Excel?
                 │
         ┌───────┴───────┐
         │ YES           │ NO
         ▼               ▼
    RAG 后端        有 GPU?
    (Excel 链路)       │
               ┌───────┴───────┐
               │ YES           │ NO
               ▼               ▼
         需要图片描述/       数据可上云?
         表格 LLM 优化?         │
               │         ┌─────┴─────┐
       ┌───────┴───┐    │ YES       │ NO
       │ YES    NO │    ▼           ▼
       ▼        ▼   RAG 后端   MinerU 原生
    RAG 后端  MinerU   (Azure)   (Pipeline CPU)
    + 本地VLM  原生
    (Hybrid)  (VLM)
```

### 8.2 混合方案 — 最佳实践

对于高质量需求, 可以将两者串联:

```
PDF
 │
 ├── MinerU Hybrid/VLM → 高精度文本 + 公式 + 阅读顺序
 │
 └── RAG 后端 (跳过 Azure DI)
      ├── Dify 增强 (图片描述 + 表格优化)
      ├── 图片相关性过滤
      ├── 超链接回插
      └── 输出 Markdown
```

```python
# 混合链: 只取 RAG 后端的内容增强部分
chain = PipelineChain.from_names([
    "image_filter",      # 过滤装饰图
    "dify_enhance",      # LLM 优化
    "hyperlink_map",     # 超链接回插
    "build_middle_json", # 重建 middle_json
    "model_output",
])

# ctx 中预填 MinerU 原生的 pages_data
ctx = PipelineContext(pdf_bytes=pdf_bytes)
ctx.pages_data = mineru_pages_data  # ← 来自 MinerU 原生
ctx = await chain.run(ctx)
```

### 8.3 总结

| 维度 | MinerU 原生 | RAG 后端 |
|------|-----------|---------|
| **文本精度** | 🟢 高 | 🟡 中高 |
| **公式精度** | 🟢 极高 | 🟡 中等 |
| **表格结构** | 🟡 中高 | 🟢 极高 |
| **图片理解** | 🔴 无 | 🟢 LLM 增强 |
| **超链接** | 🔴 丢失 | 🟢 回插 |
| **噪声过滤** | 🟡 基础 | 🟢 全面 |
| **硬件需求** | 🔴 需 GPU (VLM) | 🟢 无 GPU |
| **部署复杂度** | 🔴 高 (模型 10GB+) | 🟢 低 (轻量) |
| **离线运行** | 🟢 支持 | 🔴 需网络 |
| **运行成本** | 🟢 免费 | 🟡 ~$2/200页 |
| **可扩展性** | 🔴 需改代码 | 🟢 配置化 |
| **可观测性** | 🔴 弱 | 🟢 完整 |
| **Excel 支持** | 🔴 弱 | 🟢 专用链路 |
| **混合格式** | 🟡 手动选择后端 | 🟢 自动路由 |

> **核心差异**: MinerU 原生是**本地高精度引擎** (适合离线、公式密集型), RAG 后端是**云端智能增强管道** (适合在线、图片/表格/Excel 密集、需要 LLM 理解)。
