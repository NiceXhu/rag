# MinerU 核心处理流程详解

> 从「文件输入」到「最终输出」的完整端到端处理流程

---

## 一、整体概览

MinerU 采用 **客户端-服务端分离架构**：CLI 客户端负责文件收集与任务编排，FastAPI 服务端负责解析执行。

```
输入文件 → CLI客户端 → [本地临时服务 或 远程API] → 后端解析引擎 → 输出文件
```

---

## 二、入口：CLI 客户端 (`mineru/cli/client.py`)

### 1. 文件扫描与收集 (`collect_input_documents()`)

```
用户输入路径 (文件或目录)
    │
    ▼
扫描目录下所有文件 或 取单个文件
    │
    ▼
按后缀分类: PDF(.pdf) | 图片(.png/.jpg/...) | Office(.docx/.pptx/.xlsx)
    │
    ▼
PDF文件: 用 pypdfium2 读取实际页数 (probe_pdf_effective_pages)
图片/Office: effective_pages = 1
    │
    ▼
处理重名文件 → 去重改名 (uniquify_task_stems)
    │
    ▼
返回 InputDocument 列表 (path, suffix, stem, effective_pages, order)
```

### 2. 任务规划 (`plan_tasks()`)

```
InputDocument 列表
    │
    ▼
按 effective_pages 降序排列 (大文件优先)
    │
    ├── backend == "pipeline":
    │       使用 Bin-Packing 算法将多个小文档打包到同一批次
    │       每批总页数 ≤ processing_window_size (默认64页)
    │       超大文档(>64页)单独成批
    │
    └── 其他 backend:
           每个文档独立成批 (一对一)
    │
    ▼
返回 PlannedTask 列表 (index, documents, total_pages)
```

### 3. 并发执行 (`execute_planned_tasks()`)

```
PlannedTask 列表
    │
    ▼
创建 asyncio.Queue，按并发数 (默认3) 启动 worker 协程
每个 worker 循环从队列取任务执行 → run_planned_task()
```

### 4. 单个任务执行流程 (`run_planned_task()`)

```
单个 PlannedTask
    │
    ├── 1. 构建 multipart/form-data 请求参数 (build_request_form_data)
    │       包含: lang, backend, parse_method, formula_enable, table_enable,
    │             start_page_id, end_page_id, image_analysis,
    │             return_md, return_middle_json, return_model_output,
    │             return_content_list, return_images, response_format_zip 等
    │
    ├── 2. 提交任务到 API → POST /tasks (submit_task)
    │       上传文件 + 表单参数，获得 task_id
    │
    ├── 3. 轮询任务状态 → GET /tasks/{task_id}/status (wait_for_task_result)
    │       状态机: pending → processing → completed/failed
    │       实时渲染任务进度条
    │
    ├── 4. 下载结果 → GET /tasks/{task_id}/result (download_result_zip)
    │       下载 ZIP 包
    │
    └── 5. 解压 & 可视化 (safe_extract_zip + visualization_jobs)
            解压到 output_dir，异步生成 layout/span bbox 可视化 PDF
```

---

## 三、服务端：FastAPI (`mineru/cli/fast_api.py`)

### API 端点设计 (协议版本 v1)

```
POST /file_parse              — 同步解析 (兼容旧版插件)
POST /tasks                   — 异步任务提交
GET  /tasks/{task_id}/status  — 任务状态查询
GET  /tasks/{task_id}/result  — 结果下载
GET  /health                  — 服务健康检查
```

### 异步任务处理流程

```
POST /tasks 接收文件+参数
    │
    ▼
文件暂存到临时目录
    │
    ▼
创建 TaskRecord (task_id, status=pending, queued_ahead, ...)
    │
    ▼
BackgroundTasks 启动后台处理 → process_task_background()
    │
    ▼
通过 asyncio.Semaphore 控制并发 (默认3，可配置)
    │
    ▼
调用 aio_do_parse() → 进入核心解析引擎
```

---

## 四、核心解析引擎：`aio_do_parse()` / `do_parse()` (`mineru/cli/common.py`)

这是**整个系统的核心分发器**，所有后端路径在此汇聚。

```
文件字节流 (pdf_bytes_list) + 参数
    │
    ▼
┌── 第0步: Office 文档预处理 (_process_office_doc) ──────────────┐
│   检测文件后缀: .docx / .pptx / .xlsx                          │
│   从 pdf_bytes_list 中提取 Office 文件并单独处理                │
│   剩余 PDF/图片 继续向下流转                                    │
└────────────────────────────────────────────────────────────────┘
    │
    ▼
┌── 第1步: PDF 预处理 (_prepare_pdf_bytes) ─────────────────────┐
│   对每个 PDF 调用 convert_pdf_bytes_to_bytes():                │
│     用 pypdfium2 重写 PDF，截取指定的 start_page ~ end_page    │
│     图片文件先用 images_bytes_to_pdf_bytes() 包装为 PDF         │
└────────────────────────────────────────────────────────────────┘
    │
    ▼
┌── 第2步: 按 Backend 分发 ──────────────────────────────────────┐
│                                                                │
│    backend == "pipeline" → _process_pipeline()                 │
│    backend == "vlm-*"    → _process_vlm() / _async_process_vlm()│
│    backend == "hybrid-*" → _process_hybrid() / _async_process_hybrid()│
└────────────────────────────────────────────────────────────────┘
```

---

## 五、三大后端详细解析流程

### A. Pipeline 后端 (`backend/pipeline/pipeline_analyze.py`)

传统机器学习流水线，适合 CPU/GPU，无幻觉。

```
PDF 字节流
    │
    ▼
┌────────────── 阶段1: 图片渲染 ─────────────┐
│ pypdfium2 渲染每页为 PIL Image (DPI=200)    │
│ 支持滑动窗口 (长文档分片)                    │
│ 并发渲染: ProcessPoolExecutor (最多3进程)   │
└────────────────────────────────────────────┘
    │
    ▼
┌────────────── 阶段2: PDF 分类 ──────────────┐
│ classify() → 判断 PDF 类型:                  │
│   "ocr"  (扫描件/图片型)                      │
│   "txt"  (文字型, 文字层完整)                 │
│ 根据 parse_method (auto/txt/ocr) 决定 OCR    │
└────────────────────────────────────────────┘
    │
    ▼
┌────────────── 阶段3: 模型推理 ──────────────┐
│ ModelSingleton 单例加载模型:                  │
│                                              │
│  a) Layout Detection (PP-DocLayoutV2)        │
│     检测页面布局: 标题/正文/图片/表格/公式/    │
│     /页眉/页脚/印章 → bbox 区域列表           │
│                                              │
│  b) Formula Recognition (UniMERNet/          │
│     PP-FormulaNet-Plus-M) → LaTeX            │
│                                              │
│  c) OCR (PaddleOCR PyTorch)                  │
│     文字检测 + 识别 (109种语言)               │
│                                              │
│  d) Table Recognition (SLANet+/UNet)         │
│     表格结构识别 → HTML 表格                  │
└────────────────────────────────────────────┘
    │
    ▼
┌────────────── 阶段4: MagicModel ────────────┐
│ pipeline_magic_model.py                      │
│                                              │
│ 1. 将布局标签映射为 BlockType                 │
│ 2. Span 提取: OCR 文字行 → spans             │
│ 3. 关联 图片/表格/图表 body ↔ caption         │
│ 4. 垂直文字检测与排序                         │
│ 5. 段落分行合并 (merge_spans_to_line)         │
│ 6. 行内公式识别                               │
│ 7. 丢弃 页眉/页脚/页码等装饰元素              │
└────────────────────────────────────────────┘
    │
    ▼
┌────────────── 阶段5: middle_json ───────────┐
│ 结构化页面数据:                               │
│ { "pdf_info": [                             │
│     { "preproc_blocks": [...],              │
│       "images": [...], "tables": [...],     │
│       "text": [...], "titles": [...],       │
│       "discarded_blocks": [...],            │
│       "page_size": [w, h] }                 │
│   ] }                                       │
└────────────────────────────────────────────┘
    │
    ▼
┌────────────── 阶段6: 后处理 ────────────────┐
│ - _apply_post_ocr() 补充 OCR                │
│ - _optimize_formula_number_blocks() 公式编号│
│ - para_split() 段落拆分                      │
│ - cross_page_table_merge() 跨页表格合并     │
│ - llm_aided_title() LLM辅助标题识别          │
└────────────────────────────────────────────┘
```

### B. VLM 后端 (`backend/vlm/vlm_analyze.py`)

基于视觉语言模型，精度更高，需要 GPU。

```
PDF 字节流
    │
    ▼
┌────── 渲染图片 ───────────────────────────┐
│ pypdfium2 渲染每页 (DPI=200)              │
└──────────────────────────────────────────┘
    │
    ▼
┌────── VLM 推理 ────────────────────────────┐
│ ModelSingleton → MinerUClient              │
│                                            │
│ 模型: MinerU2.5-Pro-2604-1.2B (1.2B VLM)   │
│ 推理引擎自动选择:                            │
│   Linux   → vllm (默认) / vllm-async       │
│   Windows → lmdeploy / transformers        │
│   Mac     → mlx / transformers             │
│   Remote  → http-client (OpenAI兼容)        │
│                                            │
│ VLM 直接理解页面布局并输出结构化 JSON         │
│ 支持 图片/图表解析、跨页表格合并、           │
│ 表格内图片识别                               │
└────────────────────────────────────────────┘
    │
    ▼
┌────── middle_json 构建 ────────────────────┐
│ model_output_to_middle_json.py              │
│ → MagicModel 构建 (category分类)            │
│ → 跨页表格合并                              │
│ → 图片说明 (caption) 关联                   │
└────────────────────────────────────────────┘
```

### C. Hybrid 后端 (`backend/hybrid/hybrid_analyze.py`)

VLM + 传统 Pipeline OCR 的组合引擎，精度最高。

```
PDF 字节流
    │
    ├── VLM 推理 (页面布局分析 → blocks)
    │
    └── Pipeline 增强:
         ├── OCR 检测 (PaddleOCR 精细化文字识别)
         ├── 公式检测 (MFR: Unimernet/PP-FormulaNet)
         └── 印章检测 (seal_det_warp)
    │
    ▼
┌────── middle_json 构建 ────────────────────┐
│ 合并 VLM 布局 + Pipeline OCR/MFR 结果      │
└────────────────────────────────────────────┘
```

### D. Office 后端 (`backend/office/`)

原生文档解析，不经过 PDF 渲染，速度快数十倍。

```
.docx/.pptx/.xlsx 字节流
    │
    ├── .docx → python-docx + mammoth → blocks
    ├── .pptx → pypptx → xy_cut 排序 → blocks
    └── .xlsx → openpyxl → blocks
    │
    ▼
Office MagicModel:
  解析富文本标签 (<text style="...">, <eq>, <hyperlink>)
  表格 HTML 清洗 (仅保留 colspan/rowspan)
  嵌套列表/目录 递归解析
  body+caption 二层结构配对
```

---

## 六、输出生成：`_process_output()` (`mineru/cli/common.py`)

```
middle_json (pdf_info) + model_output
    │
    ├── 视觉验证:
    │   draw_layout_bbox() → *_layout.pdf (布局检测框)
    │   draw_span_bbox()   → *_span.pdf   (文字行框, 仅pipeline)
    │
    ├── 原始文件: *_origin.pdf / *_origin.docx
    │
    ├── union_make() → 最终输出:
    │   ├── MakeMode.MM_MD       → *.md (multimarkdown)
    │   ├── MakeMode.NLP_MD      → NLP友好纯文本
    │   ├── MakeMode.CONTENT_LIST → *_content_list.json
    │   └── MakeMode.CONTENT_LIST_V2 → *_content_list_v2.json
    │
    ├── 中间格式:
    │   ├── *_middle.json — 完整结构化解析结果
    │   └── *_model.json  — 原始模型推理结果 (调试用)
    │
    └── 图片提取:
        images/ 目录 (裁剪的图片/图表/印章)
        命名: {页码}_{区块类型}_{hash}.jpg/png
```

### 输出目录结构

```
output_dir/
  └── {文档名}/
       ├── {method}/  (pipeline/ocr/txt 或 office)
       │    ├── {文档名}.md
       │    ├── {文档名}_content_list.json
       │    ├── {文档名}_content_list_v2.json
       │    ├── {文档名}_middle.json
       │    ├── {文档名}_model.json
       │    ├── {文档名}_layout.pdf
       │    ├── {文档名}_span.pdf
       │    ├── {文档名}_origin.pdf
       │    └── images/
       │         └── {page}_{type}_{hash}.jpg
       └── ...
```

---

## 七、关键技术细节

### 模型加载策略
- **`ModelSingleton`** (线程安全单例): 所有后端都使用相同的单例模式
- 首次请求时触发模型加载，后续请求复用
- Key 根据 `(lang, formula_enable, table_enable)` 区分不同配置

### 内存管理
- **滑动窗口**: pipeline 按 `processing_window_size` (默认64页) 分片处理长文档
- **流式写入**: pipeline 批处理支持边解析边写磁盘
- **`clean_memory()`**: 每批处理完即清理显存/内存

### 并发控制
| 层级 | 机制 | 默认值 |
|------|------|--------|
| API 层 | `asyncio.Semaphore` | 3 |
| Client 层 | `asyncio.Queue` + worker 协程 | 按 server 上限 |
| Pipeline 层 | `ThreadPoolExecutor` | max_workers=1 |
| PDF 渲染 | `ProcessPoolExecutor` | max_workers=3 |

### 引擎自动选择

`get_vlm_engine()` 根据操作系统自动选择最优推理引擎:
- **Windows** → lmdeploy > transformers
- **Linux** → vllm > lmdeploy > transformers
- **macOS** → mlx > transformers
