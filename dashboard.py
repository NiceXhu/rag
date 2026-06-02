# Copyright (c) Opendatalab. All rights reserved.
"""
RAG Pipeline 可视化看板。

启动方式:
    python -m mineru.backend.rag.dashboard [--port 8765] [--cache-dir ./output/.rag_cache]

功能:
    - 列出所有 Pipeline 运行记录
    - 可视化展示各阶段状态 / 耗时 / 缓存命中
    - 查看中间结果 (Layout 框线图、Dify 对比、时间线)
    - 支持自动刷新
"""
import argparse
import asyncio
import json
import os
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

from loguru import logger

# ── HTML 模板 ──────────────────────────────────────────────

DASHBOARD_HTML = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>RAG Pipeline Dashboard</title>
<style>
*{margin:0;padding:0;box-sizing:border-box}
body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;background:#0f1117;color:#e1e4e8;min-height:100vh}
.header{background:#161b22;border-bottom:1px solid #30363d;padding:16px 24px;display:flex;align-items:center;justify-content:space-between}
.header h1{font-size:20px;font-weight:600;color:#f0f6fc}
.header .subtitle{font-size:13px;color:#8b949e;margin-top:2px}
.container{max-width:1400px;margin:0 auto;padding:24px}
/* Run Selector */
.run-selector{margin-bottom:24px;display:flex;gap:12px;align-items:center}
.run-selector select{flex:1;max-width:600px;padding:10px 14px;background:#161b22;border:1px solid #30363d;border-radius:8px;color:#e1e4e8;font-size:14px}
.run-selector button{padding:10px 20px;background:#238636;border:1px solid #2ea043;border-radius:8px;color:#fff;font-size:14px;cursor:pointer}
.run-selector button:hover{background:#2ea043}
.run-selector .btn-refresh{background:#21262d;border-color:#30363d;color:#c9d1d9}
/* Summary Cards */
.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(200px,1fr));gap:16px;margin-bottom:24px}
.card{background:#161b22;border:1px solid #30363d;border-radius:12px;padding:20px}
.card .label{font-size:12px;color:#8b949e;text-transform:uppercase;letter-spacing:.5px}
.card .value{font-size:28px;font-weight:700;margin-top:4px}
.card .unit{font-size:14px;font-weight:400;color:#8b949e}
.status-completed{color:#3fb950}
.status-failed{color:#f85149}
.status-running{color:#d29922}
.status-cached{color:#58a6ff}
/* Pipeline Flow */
.flow{margin-bottom:24px}
.flow h2{font-size:16px;margin-bottom:16px;color:#f0f6fc}
.flow-stages{display:flex;gap:0;align-items:stretch;overflow-x:auto}
.stage-card{flex:1;min-width:180px;background:#161b22;border:1px solid #30363d;border-radius:12px;padding:16px;position:relative;margin-right:-8px;transition:transform .2s}
.stage-card:hover{transform:translateY(-2px);z-index:2}
.stage-card .stage-num{font-size:11px;color:#8b949e;margin-bottom:4px}
.stage-card .stage-name{font-size:14px;font-weight:600;margin-bottom:8px}
.stage-card .stage-time{font-size:22px;font-weight:700}
.stage-card .stage-summary{font-size:12px;color:#8b949e;margin-top:6px}
.stage-arrow{display:flex;align-items:center;font-size:24px;color:#30363d;margin:0 4px}
/* Timeline Bars */
.timeline{margin-bottom:24px}
.timeline h2{font-size:16px;margin-bottom:12px;color:#f0f6fc}
.timeline-bar{display:flex;align-items:center;margin-bottom:8px;gap:12px}
.timeline-bar .bar-label{width:140px;font-size:13px;color:#8b949e;text-align:right;flex-shrink:0}
.timeline-bar .bar-track{flex:1;height:24px;background:#21262d;border-radius:6px;overflow:hidden;position:relative}
.timeline-bar .bar-fill{height:100%;border-radius:6px;display:flex;align-items:center;padding:0 8px;font-size:11px;font-weight:600;transition:width .6s ease}
.bar-completed{background:linear-gradient(90deg,#238636,#3fb950)}
.bar-cached{background:linear-gradient(90deg,#1f6feb,#58a6ff)}
.bar-failed{background:linear-gradient(90deg,#da3633,#f85149)}
.bar-skipped{background:linear-gradient(90deg,#484f58,#6e7681)}
.timeline-bar .bar-time{margin-left:8px;font-size:12px;color:#8b949e;width:60px;text-align:right}
/* Viz Gallery */
.gallery{margin-bottom:24px}
.gallery h2{font-size:16px;margin-bottom:12px;color:#f0f6fc}
.gallery-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(300px,1fr));gap:16px}
.gallery-item{background:#161b22;border:1px solid #30363d;border-radius:12px;overflow:hidden}
.gallery-item img{width:100%;display:block}
.gallery-item .caption{padding:10px 14px;font-size:12px;color:#8b949e}
.gallery-item iframe{width:100%;height:300px;border:0;background:#fff}
/* Empty State */
.empty{text-align:center;padding:80px 20px;color:#484f58}
.empty .icon{font-size:64px;margin-bottom:16px}
.empty p{font-size:16px}
/* Footer */
.footer{padding:16px 24px;border-top:1px solid #30363d;font-size:12px;color:#484f58;display:flex;justify-content:space-between}
.footer .dot{display:inline-block;width:8px;height:8px;border-radius:50%;margin-right:6px}
.dot-live{background:#3fb950}
</style>
</head>
<body>

<div class="header">
  <div>
    <h1>RAG Pipeline Dashboard</h1>
    <div class="subtitle">Azure DI + Dify Workflow · 处理过程可视化</div>
  </div>
  <div style="display:flex;gap:8px;align-items:center">
    <span id="autoRefreshStatus" style="font-size:12px;color:#8b949e"></span>
    <button class="btn-refresh" onclick="toggleAutoRefresh()" id="btnAutoRefresh">⏱ 自动刷新</button>
    <button onclick="loadRuns()">🔄 刷新列表</button>
  </div>
</div>

<div class="container">
  <!-- Run Selector -->
  <div class="run-selector">
    <select id="runSelect" onchange="loadRun(this.value)"><option value="">-- 选择 Pipeline 运行 --</option></select>
    <button onclick="loadRun(document.getElementById('runSelect').value)">📊 加载</button>
  </div>

  <!-- Summary Cards -->
  <div class="cards" id="summaryCards"></div>

  <!-- Pipeline Stage Flow -->
  <div class="flow">
    <h2>📋 处理流水线</h2>
    <div class="flow-stages" id="stageFlow"></div>
  </div>

  <!-- Timeline -->
  <div class="timeline">
    <h2>⏱ 阶段耗时 (Gantt)</h2>
    <div id="timelineBars"></div>
  </div>

  <!-- Visualizations -->
  <div class="gallery">
    <h2>🖼 中间结果可视化</h2>
    <div class="gallery-grid" id="vizGallery"></div>
  </div>

  <!-- Empty State -->
  <div class="empty" id="emptyState">
    <div class="icon">📂</div>
    <p>选择一个 Pipeline 运行记录来查看处理详情</p>
    <p style="font-size:13px;color:#484f58;margin-top:8px">
      确保 .rag_cache 目录存在于 output 路径下
    </p>
  </div>
</div>

<div class="footer">
  <span>MinerU RAG Pipeline</span>
  <span id="footerTime"></span>
</div>

<script>
// ── 全局状态 ──
let currentRunId = null;
let autoRefresh = false;
let autoRefreshTimer = null;
const API_BASE = '';

// ── API ──
async function api(path) {
  const resp = await fetch(API_BASE + path);
  if (!resp.ok) throw new Error(`${resp.status} ${resp.statusText}`);
  return resp.json();
}

// ── 加载运行列表 ──
async function loadRuns() {
  try {
    const data = await api('/api/runs');
    const select = document.getElementById('runSelect');
    select.innerHTML = '<option value="">-- 选择 Pipeline 运行 --</option>';
    for (const run of data.runs || []) {
      const opt = document.createElement('option');
      opt.value = run.run_id;
      opt.textContent = `${run.doc_stem} — ${run.status} — ${run.total_duration_s?.toFixed(1)}s — ${run.finished_at?.slice(0,19) || '?'}`;
      select.appendChild(opt);
    }
    document.getElementById('footerTime').textContent =
      `上次更新: ${new Date().toLocaleTimeString()} · ${data.runs?.length || 0} 条记录`;
  } catch (e) {
    console.error('Failed to load runs:', e);
  }
}

// ── 加载单个运行 ──
async function loadRun(runId) {
  if (!runId) return;
  currentRunId = runId;
  try {
    const report = await api(`/api/runs/${runId}`);
    render(report);
    document.getElementById('emptyState').style.display = 'none';
  } catch (e) {
    console.error('Failed to load run:', e);
    alert('加载失败: ' + e.message);
  }
}

// ── 渲染 ──
function render(report) {
  renderSummaryCards(report);
  renderStageFlow(report);
  renderTimeline(report);
  renderVizGallery(report);
}

// ── 摘要卡片 ──
function renderSummaryCards(r) {
  const statusClass = r.status === 'completed' ? 'status-completed' :
                      r.status === 'failed' ? 'status-failed' : 'status-running';
  const stages = r.stages || {};
  const totalStages = Object.keys(stages).length;
  const completedStages = Object.values(stages).filter(s =>
    s.status === 'completed' || s.status === 'cached').length;
  const cacheHits = r.cache_hits || 0;

  document.getElementById('summaryCards').innerHTML = `
    <div class="card">
      <div class="label">状态</div>
      <div class="value ${statusClass}">${statusIcon(r.status)} ${r.status || '?'}</div>
    </div>
    <div class="card">
      <div class="label">总耗时</div>
      <div class="value">${r.total_duration_s?.toFixed(1) || '?'}<span class="unit">s</span></div>
      <div class="stage-summary">${r.doc_stem || ''}</div>
    </div>
    <div class="card">
      <div class="label">阶段进度</div>
      <div class="value">${completedStages}<span class="unit">/${totalStages}</span></div>
      <div class="stage-summary">${completedStages === totalStages ? '✅ 全部完成' : '🔄 进行中'}</div>
    </div>
    <div class="card">
      <div class="label">缓存命中</div>
      <div class="value status-cached">${cacheHits}<span class="unit">/${totalStages}</span></div>
      <div class="stage-summary">💾 checkpoint 复用</div>
    </div>
  `;
}

function statusIcon(status) {
  const map = {completed:'✅',cached:'💾',failed:'❌',running:'🔄',pending:'⏳',skipped:'⏭️'};
  return map[status] || '❓';
}

// ── 阶段流程 ──
function renderStageFlow(r) {
  const stages = r.stages || {};
  const names = Object.keys(stages);
  if (!names.length) {
    document.getElementById('stageFlow').innerHTML = '<div style="padding:20px;color:#484f58">无阶段数据</div>';
    return;
  }

  let html = '';
  names.forEach((name, i) => {
    const s = stages[name];
    const timeColor = s.status === 'failed' ? '#f85149' :
                      s.status === 'cached' ? '#58a6ff' :
                      s.status === 'completed' ? '#3fb950' : '#8b949e';
    html += `
      <div class="stage-card" style="border-top:3px solid ${timeColor}">
        <div class="stage-num">Stage ${i + 1}/${names.length}</div>
        <div class="stage-name">${statusIcon(s.status)} ${name}</div>
        <div class="stage-time" style="color:${timeColor}">${s.duration_s?.toFixed(2) || '0'}s</div>
        <div class="stage-summary">
          ${s.from_cache ? '💾 缓存恢复' : s.status === 'skipped' ? '⏭️ 跳过' : ''}
          ${s.output_summary?.summary || formatOutputSummary(s.output_summary) || ''}
        </div>
      </div>`;
    if (i < names.length - 1) {
      html += '<div class="stage-arrow">→</div>';
    }
  });
  document.getElementById('stageFlow').innerHTML = html;
}

function formatOutputSummary(summary) {
  if (!summary) return '';
  const parts = [];
  if (summary.pages) parts.push(`${summary.pages} pages`);
  if (summary.paragraphs) parts.push(`${summary.paragraphs} paras`);
  if (summary.tables) parts.push(`${summary.tables} tables`);
  if (summary.figures) parts.push(`${summary.figures} figures`);
  if (summary.images) parts.push(`${summary.images} img enhanced`);
  if (summary.page_count) parts.push(`${summary.page_count} pages`);
  return parts.join(', ');
}

// ── 时间线 ──
function renderTimeline(r) {
  const stages = r.stages || {};
  const names = Object.keys(stages);
  if (!names.length) {
    document.getElementById('timelineBars').innerHTML = '';
    return;
  }

  // 模拟 Gantt 图: 每个阶段按顺序排列
  const totalTime = r.total_duration_s || 1;
  let html = '';

  names.forEach(name => {
    const s = stages[name];
    const pct = Math.max(s.duration_s / totalTime * 100, 1);
    const barClass = s.status === 'completed' ? 'bar-completed' :
                     s.status === 'cached' ? 'bar-cached' :
                     s.status === 'failed' ? 'bar-failed' : 'bar-skipped';

    html += `
      <div class="timeline-bar">
        <div class="bar-label">${name}</div>
        <div class="bar-track">
          <div class="bar-fill ${barClass}" style="width:${pct.toFixed(1)}%">
            ${s.from_cache ? '💾' : ''} ${s.status}
          </div>
        </div>
        <div class="bar-time">${s.duration_s?.toFixed(1)}s</div>
      </div>`;
  });

  // 总时间参考线
  html += `
    <div class="timeline-bar" style="margin-top:12px;border-top:1px dashed #30363d;padding-top:12px">
      <div class="bar-label" style="color:#f0f6fc;font-weight:600">Total</div>
      <div class="bar-track">
        <div class="bar-fill bar-completed" style="width:100%">${r.total_duration_s?.toFixed(1)}s</div>
      </div>
      <div class="bar-time" style="color:#f0f6fc;font-weight:600">${r.total_duration_s?.toFixed(1)}s</div>
    </div>`;

  document.getElementById('timelineBars').innerHTML = html;
}

// ── 可视化 Gallery ──
function renderVizGallery(r) {
  const runId = r.run_id || currentRunId;
  if (!runId) {
    document.getElementById('vizGallery').innerHTML = '';
    return;
  }

  // 通过 API 获取文件列表
  fetch(`${API_BASE}/api/runs/${runId}/files`)
    .then(r => r.json())
    .then(data => {
      const files = data.files || [];
      if (!files.length) {
        document.getElementById('vizGallery').innerHTML =
          '<div style="padding:20px;color:#484f58">暂无可视化文件</div>';
        return;
      }

      let html = '';
      for (const f of files) {
        const url = `${API_BASE}/api/runs/${runId}/file/${encodeURIComponent(f.name)}`;
        if (f.name.endsWith('.png') || f.name.endsWith('.jpg')) {
          html += `
            <div class="gallery-item">
              <img src="${url}" alt="${f.name}" loading="lazy">
              <div class="caption">${f.name}</div>
            </div>`;
        } else if (f.name.endsWith('.txt')) {
          html += `
            <div class="gallery-item">
              <iframe src="${url}" title="${f.name}"></iframe>
              <div class="caption">${f.name}</div>
            </div>`;
        }
      }
      document.getElementById('vizGallery').innerHTML = html || '<div style="padding:20px;color:#484f58">无可预览文件</div>';
    })
    .catch(() => {
      document.getElementById('vizGallery').innerHTML =
        '<div style="padding:20px;color:#484f58">加载可视化文件失败</div>';
    });
}

// ── 自动刷新 ──
function toggleAutoRefresh() {
  autoRefresh = !autoRefresh;
  const btn = document.getElementById('btnAutoRefresh');
  const status = document.getElementById('autoRefreshStatus');
  if (autoRefresh) {
    btn.textContent = '⏸ 停止刷新';
    status.innerHTML = '<span class="dot dot-live"></span> 自动刷新中 (10s)';
    autoRefreshTimer = setInterval(() => {
      loadRuns();
      if (currentRunId) loadRun(currentRunId);
    }, 10000);
  } else {
    btn.textContent = '⏱ 自动刷新';
    status.textContent = '';
    if (autoRefreshTimer) clearInterval(autoRefreshTimer);
  }
}

// ── 初始化 ──
loadRuns();
document.getElementById('footerTime').textContent =
  '就绪 · ' + new Date().toLocaleTimeString();
</script>
</body>
</html>"""

# ── HTTP 服务器 ──────────────────────────────────────────

from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, unquote


class DashboardHandler(BaseHTTPRequestHandler):
    """看板 HTTP 请求处理器"""

    cache_dir: Path = Path(".")
    runs_cache: dict = {}
    runs_cache_time: float = 0
    cache_ttl: float = 5.0  # 运行列表缓存 5 秒

    def log_message(self, format, *args):
        logger.debug(f"[Dashboard] {args[0]}")

    def _send_json(self, data, status=200):
        body = json.dumps(data, ensure_ascii=False, indent=2).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_html(self, html: str):
        body = html.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_file(self, path: Path):
        if not path.exists() or not path.is_file():
            self._send_json({"error": "file not found"}, 404)
            return

        # MIME 猜测
        suffix = path.suffix.lower()
        mime_map = {
            ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
            ".gif": "image/gif", ".svg": "image/svg+xml",
            ".txt": "text/plain; charset=utf-8", ".json": "application/json",
            ".html": "text/html", ".pdf": "application/pdf",
        }
        content_type = mime_map.get(suffix, "application/octet-stream")

        body = path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "public, max-age=60")
        self.end_headers()
        self.wfile.write(body)

    def _list_runs(self) -> list[dict]:
        """扫描 .rag_cache 目录, 列出所有运行记录"""
        now = time.time()
        if self.runs_cache and (now - self.runs_cache_time) < self.cache_ttl:
            return list(self.runs_cache.values())

        runs = []
        cache_root = self.cache_dir

        if cache_root.exists():
            for run_dir in sorted(cache_root.iterdir(), reverse=True):
                if not run_dir.is_dir():
                    continue
                report_path = run_dir / "pipeline_run.json"
                if not report_path.exists():
                    continue

                try:
                    report = json.loads(report_path.read_text("utf-8"))
                    runs.append({
                        "run_id": report.get("run_id", run_dir.name),
                        "doc_stem": report.get("doc_stem", ""),
                        "status": report.get("status", "unknown"),
                        "total_duration_s": report.get("total_duration_s", 0),
                        "finished_at": report.get("finished_at", ""),
                        "cache_hits": report.get("cache_hits", 0),
                        "content_hash": report.get("content_hash", run_dir.name),
                    })
                except (json.JSONDecodeError, OSError):
                    continue

        self.runs_cache = {r["content_hash"]: r for r in runs}
        self.runs_cache_time = now
        return runs

    def _list_viz_files(self, run_id: str) -> list[dict]:
        """列出某个运行的 visualization 文件"""
        run_dir = None
        for d in self.cache_dir.iterdir():
            if d.is_dir():
                rp = d / "pipeline_run.json"
                if rp.exists():
                    try:
                        r = json.loads(rp.read_text("utf-8"))
                        if r.get("content_hash") == run_id or r.get("run_id") == run_id:
                            run_dir = d
                            break
                    except Exception:
                        pass

        if run_dir is None:
            return []

        viz_dir = run_dir / "visualizations"
        if not viz_dir.exists():
            return []

        files = []
        for f in sorted(viz_dir.iterdir()):
            if f.is_file():
                # 生成相对路径标识
                files.append({
                    "name": f.name,
                    "size": f.stat().st_size,
                    "relative": f"visualizations/{f.name}",
                })
        return files

    # ── 路由 ──

    def do_GET(self):
        parsed = urlparse(self.path)
        path = unquote(parsed.path)

        try:
            # 主页
            if path == "/" or path == "/index.html":
                self._send_html(DASHBOARD_HTML)

            # API: 运行列表
            elif path == "/api/runs":
                runs = self._list_runs()
                self._send_json({"runs": runs, "cache_dir": str(self.cache_dir)})

            # API: 运行详情
            elif path.startswith("/api/runs/") and not path.endswith("/files"):
                run_id = path.split("/api/runs/")[1].split("/")[0]
                # 查找对应的 pipeline_run.json
                report = None
                for d in self.cache_dir.iterdir():
                    if d.is_dir():
                        rp = d / "pipeline_run.json"
                        if rp.exists():
                            try:
                                r = json.loads(rp.read_text("utf-8"))
                                if r.get("content_hash") == run_id or r.get("run_id") == run_id:
                                    report = r
                                    break
                            except Exception:
                                pass
                if report:
                    self._send_json(report)
                else:
                    self._send_json({"error": "run not found"}, 404)

            # API: 运行的可视化文件列表
            elif path.endswith("/files"):
                run_id = path.split("/api/runs/")[1].split("/files")[0]
                files = self._list_viz_files(run_id)
                self._send_json({"files": files, "run_id": run_id})

            # API: 可视化文件内容
            elif "/file/" in path:
                parts = path.split("/file/", 1)
                run_id = parts[0].split("/api/runs/")[1].rstrip("/")
                file_rel = parts[1]

                # 查找文件
                found = None
                for d in self.cache_dir.iterdir():
                    if d.is_dir():
                        rp = d / "pipeline_run.json"
                        if rp.exists():
                            try:
                                r = json.loads(rp.read_text("utf-8"))
                                if r.get("content_hash") == run_id or r.get("run_id") == run_id:
                                    candidate = d / file_rel
                                    if candidate.exists():
                                        found = candidate
                                        break
                            except Exception:
                                pass
                if found:
                    self._send_file(found)
                else:
                    self._send_json({"error": "file not found"}, 404)

            else:
                self._send_json({"error": "not found"}, 404)

        except Exception as e:
            logger.exception(f"[Dashboard] Error handling {path}")
            self._send_json({"error": str(e)}, 500)


# ── 启动入口 ──────────────────────────────────────────────

def start_dashboard(
    cache_dir: str | Path = ".",
    port: int = 8765,
    open_browser: bool = True,
) -> HTTPServer:
    """
    启动看板 HTTP 服务。

    Args:
        cache_dir: .rag_cache 目录所在的根路径 (output 目录)
        port: 监听端口
        open_browser: 是否自动打开浏览器

    Returns:
        HTTPServer 实例
    """
    cache_path = Path(cache_dir).resolve()
    rag_cache = cache_path / ".rag_cache"

    # 注入配置到 handler
    DashboardHandler.cache_dir = rag_cache

    server = HTTPServer(("0.0.0.0", port), DashboardHandler)

    logger.info(f"Dashboard: http://localhost:{port}")
    logger.info(f"Cache dir: {rag_cache}")

    if open_browser:
        import webbrowser
        threading.Timer(0.5, lambda: webbrowser.open(f"http://localhost:{port}")).start()

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        logger.info("Dashboard shutting down...")
        server.shutdown()

    return server


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="RAG Pipeline Dashboard")
    parser.add_argument("--port", type=int, default=8765, help="监听端口 (默认 8765)")
    parser.add_argument("--cache-dir", type=str, default=".",
                        help="包含 .rag_cache 的输出目录 (默认当前目录)")
    parser.add_argument("--no-browser", action="store_true", help="不自动打开浏览器")
    args = parser.parse_args()

    start_dashboard(
        cache_dir=args.cache_dir,
        port=args.port,
        open_browser=not args.no_browser,
    )
