#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
简易图床 (imghost) —— 单文件、零依赖、无需登录
把图片拖到网页上，自动生成直链，复制到任何地方用。

用法:
    python imghost.py                      # 默认 0.0.0.0:8899, 存到 ./images
    python imghost.py --port 8000
    python imghost.py --dir D:\\图片库 --port 80
    python imghost.py --max-mb 100 --open

只依赖 Python 标准库，Windows / Linux / macOS 都能跑。
"""

import argparse
import json
import mimetypes
import os
import re
import socket
import sys
import threading
import time
import urllib.parse
import uuid
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

VERSION = "1.2"

mimetypes.add_type("image/webp", ".webp")
mimetypes.add_type("image/avif", ".avif")
mimetypes.add_type("image/jxl", ".jxl")
mimetypes.add_type("image/heic", ".heic")
mimetypes.add_type("image/svg+xml", ".svg")
mimetypes.add_type("video/mp4", ".mp4")
mimetypes.add_type("video/webm", ".webm")
mimetypes.add_type("application/pdf", ".pdf")

WIN_RESERVED = {"con", "prn", "aux", "nul"}
WIN_RESERVED |= {"com%d" % i for i in range(1, 10)}
WIN_RESERVED |= {"lpt%d" % i for i in range(1, 10)}
RESERVED_NAMES = {"index.html", "favicon.ico", "robots.txt"}


# --------------------------------------------------------------------------
# 工具函数
# --------------------------------------------------------------------------
def safe_name(raw: str) -> str:
    """把任意文件名清洗成本地安全的名字, 同时尽量保留中文和原貌。"""
    name = (raw or "").replace("\\", "/").split("/")[-1]
    name = re.sub(r'[\x00-\x1f<>:"/\\|?*]', "_", name)
    name = name.strip(" .")
    if not name:
        name = "image"

    stem, ext = os.path.splitext(name)
    if stem.lower() in WIN_RESERVED:
        stem = "_" + stem
    if len(stem) > 100:
        stem = stem[:100]
    if len(ext) > 16:
        ext = ext[:16]
    name = stem + ext

    low = name.lower()
    if low in RESERVED_NAMES or low == "api" or low.startswith("api."):
        name = "_" + name
    return name


def reserve_unique(dirpath: str, name: str):
    """原子地占住一个不重名的文件名, 返回 (最终名字, 完整路径)。

    用 O_EXCL 创建空文件占位, 这样两个人同时上传同名文件时
    不会算出同一个名字从而互相覆盖。
    """
    stem, ext = os.path.splitext(name)
    i = 0
    while True:
        cand = name if i == 0 else "%s-%d%s" % (stem, i, ext)
        full = os.path.join(dirpath, cand)
        try:
            fd = os.open(full, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o666)
        except FileExistsError:
            i += 1
            if i > 10000:
                raise OSError("同名文件过多，无法分配文件名")
            continue
        os.close(fd)
        return cand, full



def lan_ips():
    """尽力找出本机局域网 IPv4 地址。"""
    ips = []
    try:
        hostname = socket.gethostname()
        for info in socket.getaddrinfo(hostname, None, socket.AF_INET):
            ip = info[4][0]
            if ip not in ips and not ip.startswith("127."):
                ips.append(ip)
    except Exception:
        pass
    if not ips:
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.connect(("8.8.8.8", 80))
            ips.append(s.getsockname()[0])
            s.close()
        except Exception:
            pass
    return ips


def human_size(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            if unit == "B":
                return "%d %s" % (n, unit)
            return "%.1f %s" % (n, unit)
        n /= 1024.0
    return "%d B" % n


# --------------------------------------------------------------------------
# 配置
# --------------------------------------------------------------------------
class Config:
    def __init__(self, root, port, bind, max_mb, cdn_base, title, readonly):
        self.root = os.path.abspath(root)
        self.port = port
        self.bind = bind
        self.max_bytes = int(max_mb * 1024 * 1024)
        self.cdn_base = (cdn_base or "").rstrip("/")
        self.title = title
        self.readonly = readonly
        self.base_url = ""


CFG = None  # type: Config


# --------------------------------------------------------------------------
# 网页界面
# --------------------------------------------------------------------------
PAGE = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>__TITLE__</title>
<link rel="icon" href="data:image/svg+xml,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 100 100'><text y='.9em' font-size='90'>🖼️</text></svg>">
<style>
:root{
  --bg:#0f1115; --panel:#171a21; --panel2:#1e222b; --line:#2a2f3a;
  --fg:#e6e9ef; --dim:#98a1b3; --accent:#4c8dff; --ok:#31c48d; --danger:#f05252;
}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);
  font:14px/1.6 -apple-system,BlinkMacSystemFont,"Segoe UI","Microsoft YaHei",sans-serif}
.wrap{max-width:1200px;margin:0 auto;padding:24px 20px 80px}
header{display:flex;align-items:center;justify-content:space-between;gap:16px;flex-wrap:wrap;margin-bottom:18px}
h1{font-size:19px;margin:0;font-weight:600;letter-spacing:.3px}
h1 span{color:var(--dim);font-weight:400;font-size:13px;margin-left:8px}
.stats{color:var(--dim);font-size:13px}
.stats b{color:var(--fg);font-weight:600}

#drop{border:2px dashed var(--line);border-radius:14px;background:var(--panel);
  padding:40px 20px;text-align:center;transition:.15s;cursor:pointer;user-select:none}
#drop:hover{border-color:#3d4657;background:var(--panel2)}
#drop.hot{border-color:var(--accent);background:rgba(76,141,255,.09)}
#drop .big{font-size:36px;line-height:1;margin-bottom:10px}
#drop .t1{font-size:15px;font-weight:600}
#drop .t2{color:var(--dim);font-size:13px;margin-top:6px}
#drop .t2 kbd{background:#242936;border:1px solid var(--line);border-radius:4px;
  padding:1px 5px;font:12px ui-monospace,monospace}
#bar{margin-top:12px;height:4px;border-radius:2px;background:#242936;overflow:hidden;display:none}
#bar.on{display:block}
#bar>i{display:block;height:100%;width:0;background:var(--accent);transition:width .2s}

.toolbar{display:flex;gap:10px;align-items:center;margin:22px 0 14px;flex-wrap:wrap}
input[type=search]{flex:1;min-width:180px;background:var(--panel);border:1px solid var(--line);
  color:var(--fg);border-radius:9px;padding:9px 13px;font-size:14px;outline:none}
input[type=search]:focus{border-color:var(--accent)}
button{font:inherit;border:1px solid var(--line);background:var(--panel2);color:var(--fg);
  border-radius:8px;padding:7px 13px;cursor:pointer;transition:.12s;white-space:nowrap}
button:hover{border-color:#3d4657;background:#252a35}
button.primary{background:var(--accent);border-color:var(--accent);color:#fff}
button.primary:hover{background:#3f7cea}
button.danger:hover{border-color:var(--danger);color:var(--danger)}

.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(210px,1fr));gap:14px}
.card{background:var(--panel);border:1px solid var(--line);border-radius:12px;overflow:hidden;
  display:flex;flex-direction:column;transition:.15s}
.card:hover{border-color:#3d4657;transform:translateY(-1px)}
.thumb{height:132px;background:#0b0d11 center/cover no-repeat;display:block;position:relative;cursor:zoom-in}
.thumb img{width:100%;height:100%;object-fit:cover;display:block}
.thumb .badge{position:absolute;top:8px;right:8px;background:rgba(0,0,0,.62);color:#fff;
  font-size:11px;padding:2px 7px;border-radius:20px;backdrop-filter:blur(2px)}
.meta{padding:9px 11px 0;min-width:0}
.name{font-size:13px;font-weight:500;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.sub{color:var(--dim);font-size:11.5px;margin-top:1px}
.acts{display:flex;gap:6px;padding:9px 11px 11px}
.acts button{flex:1;padding:6px 4px;font-size:12px;border-radius:7px}
.empty{color:var(--dim);text-align:center;padding:50px 0;font-size:14px}

#toast{position:fixed;left:50%;bottom:32px;transform:translate(-50%,20px);background:#0a1f16;
  border:1px solid #1f6b4d;color:#c7f5e2;padding:11px 20px;border-radius:10px;font-size:13.5px;
  opacity:0;pointer-events:none;transition:.22s;z-index:99;max-width:calc(100vw - 40px);
  overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
#toast.show{opacity:1;transform:translate(-50%,0)}
#toast.err{background:#210b0b;border-color:#6b1f1f;color:#ffd7d7}

#lb{position:fixed;inset:0;background:rgba(6,8,12,.93);display:none;align-items:center;
  justify-content:center;z-index:100;padding:28px;cursor:zoom-out}
#lb.on{display:flex}
#lb img{max-width:100%;max-height:100%;object-fit:contain;border-radius:6px}
footer{position:fixed;bottom:0;left:0;right:0;padding:7px 20px;background:#0b0d11;
  border-top:1px solid var(--line);color:var(--dim);font-size:12px;
  display:flex;justify-content:space-between;gap:12px;flex-wrap:wrap}
footer code{color:#c9d4e8;font-family:ui-monospace,Consolas,monospace}
@media(max-width:520px){
  .wrap{padding:16px 12px 80px}
  .grid{grid-template-columns:repeat(auto-fill,minmax(150px,1fr));gap:10px}
  .thumb{height:110px}
}
</style>
</head>
<body>
<div class="wrap">
  <header>
    <h1>🖼️ __TITLE__<span>拖进来就能用</span></h1>
    <div class="stats" id="stats"></div>
  </header>

  <div id="drop">
    <div class="big">⬆️</div>
    <div class="t1">把图片拖到这里</div>
    <div class="t2">或点击选择文件 / 按 <kbd>Ctrl</kbd>+<kbd>V</kbd> 粘贴剪贴板截图</div>
    <div id="bar"><i></i></div>
  </div>

  <div class="toolbar">
    <input type="search" id="q" placeholder="搜索文件名…">
    <button id="refresh">刷新</button>
  </div>

  <div class="grid" id="grid"></div>
  <div class="empty" id="empty" style="display:none">还没有图片，往上面拖一张试试。</div>
</div>

<footer>
  <span>直链前缀 <code id="baseUrl"></code></span>
  <span>图片目录 <code>__ROOT__</code></span>
</footer>

<div id="toast"></div>
<div id="lb"><img alt=""></div>
<input type="file" id="file" multiple accept="image/*" hidden>

<script>
const INJECTED_BASE = "__BASE_URL__";
const READONLY = __READONLY__;
const MAX_MB = __MAX_MB__;
const $ = s => document.querySelector(s);
let ITEMS = [];

/* ---------- 直链前缀 ---------- */
function baseUrl(){
  // 配成相对前缀（如 /imgs）时永远优先用，这样链接和页面天然同源，
  // https 页面里也不会触发混合内容拦截。
  if (INJECTED_BASE.charAt(0) === '/') return INJECTED_BASE;
  const h = location.hostname;
  if (h === 'localhost' || h === '127.0.0.1' || h === '::1' || h === '[::1]') {
    return INJECTED_BASE || location.origin;
  }
  return location.origin;
}
function linkFor(name){ return baseUrl() + '/' + encodeURIComponent(name); }

/* ---------- 提示 ---------- */
let toastTimer;
function toast(msg, isErr){
  const t = $('#toast');
  t.textContent = msg;
  t.className = 'show' + (isErr ? ' err' : '');
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { t.className = isErr ? 'err' : ''; }, 2600);
}

/* ---------- 复制 (http 下 navigator.clipboard 不可用, 必须留降级) ---------- */
async function copyText(text){
  if (window.isSecureContext && navigator.clipboard) {
    try { await navigator.clipboard.writeText(text); return true; } catch(e){}
  }
  const ta = document.createElement('textarea');
  ta.value = text;
  ta.setAttribute('readonly', '');
  ta.style.cssText = 'position:fixed;top:0;left:0;opacity:0;pointer-events:none';
  document.body.appendChild(ta);
  ta.select();
  ta.setSelectionRange(0, ta.value.length);
  let ok = false;
  try { ok = document.execCommand('copy'); } catch(e){ ok = false; }
  document.body.removeChild(ta);
  if (!ok) window.prompt('自动复制被浏览器拦住了，请手动复制：', text);
  return ok;
}

/* ---------- 列表 ---------- */
async function refresh(){
  try {
    const r = await fetch('/api/list', {cache:'no-store'});
    const d = await r.json();
    ITEMS = d.items || [];
    render();
  } catch(e) { toast('读取列表失败：' + e.message, true); }
}

function render(){
  const kw = $('#q').value.trim().toLowerCase();
  const list = kw ? ITEMS.filter(i => i.name.toLowerCase().includes(kw)) : ITEMS;
  const grid = $('#grid');
  grid.innerHTML = '';
  $('#empty').style.display = list.length ? 'none' : 'block';
  $('#empty').textContent = ITEMS.length
      ? '没有匹配的图片。'
      : '还没有图片，往上面拖一张试试。';

  let total = 0;
  ITEMS.forEach(i => total += i.size);
  $('#stats').innerHTML = '共 <b>' + ITEMS.length + '</b> 张 · <b>' + fmt(ITEMS.length && total) + '</b>';

  for (const it of list) grid.appendChild(card(it));
}

function fmt(n){
  if (!n) return '0 B';
  const u = ['B','KB','MB','GB'];
  let i = 0;
  while (n >= 1024 && i < u.length - 1) { n /= 1024; i++; }
  return (i === 0 ? n : n.toFixed(1)) + ' ' + u[i];
}

function card(it){
  const el = document.createElement('div');
  el.className = 'card';

  const thumb = document.createElement('a');
  thumb.className = 'thumb';
  thumb.href = linkFor(it.name);
  thumb.target = '_blank';
  thumb.rel = 'noopener';
  if (it.isImage) {
    const img = document.createElement('img');
    img.src = linkFor(it.name);
    img.loading = 'lazy';
    img.alt = it.name;
    thumb.appendChild(img);
  } else {
    const b = document.createElement('span');
    b.className = 'badge';
    b.textContent = (it.name.split('.').pop() || 'file').toUpperCase();
    thumb.appendChild(b);
  }
  el.appendChild(thumb);

  const meta = document.createElement('div');
  meta.className = 'meta';
  const nm = document.createElement('div');
  nm.className = 'name';
  nm.textContent = it.name;
  nm.title = it.name;
  const sub = document.createElement('div');
  sub.className = 'sub';
  sub.textContent = fmt(it.size) + ' · ' + it.time;
  meta.appendChild(nm);
  meta.appendChild(sub);
  el.appendChild(meta);

  const acts = document.createElement('div');
  acts.className = 'acts';

  const btnCopy = document.createElement('button');
  btnCopy.textContent = '复制链接';
  btnCopy.onclick = async () => {
    const url = linkFor(it.name);
    const ok = await copyText(url);
    toast(ok ? '已复制：' + url : '复制失败，请手动复制');
  };

  const btnOpen = document.createElement('button');
  btnOpen.textContent = '打开';
  btnOpen.onclick = () => window.open(linkFor(it.name), '_blank', 'noopener');

  acts.appendChild(btnCopy);
  acts.appendChild(btnOpen);

  if (!READONLY) {
    const btnDel = document.createElement('button');
    btnDel.className = 'danger';
    btnDel.textContent = '删除';
    btnDel.onclick = async () => {
      if (!confirm('确定删除「' + it.name + '」？此操作不可撤销。')) return;
      const r = await fetch('/api/delete?name=' + encodeURIComponent(it.name), {method:'DELETE'});
      if (r.ok) { toast('已删除'); refresh(); }
      else { toast('删除失败', true); }
    };
    acts.appendChild(btnDel);
  }
  el.appendChild(acts);
  return el;
}

/* ---------- 上传 ---------- */
let busy = 0, doneCount = 0;

async function uploadFiles(files){
  const list = Array.from(files || []).filter(Boolean);
  if (!list.length) return;
  if (READONLY) { toast('服务器已设置为只读', true); return; }

  busy += list.length;
  doneCount = 0;
  const bar = $('#bar');
  bar.classList.add('on');
  bar.querySelector('i').style.width = '0%';

  let okCount = 0, failMsg = '';
  for (const f of list) {
    try {
      if (f.size > MAX_MB * 1024 * 1024) {
        failMsg = f.name + ' 超过 ' + MAX_MB + 'MB';
        continue;
      }
      const name = f.name || ('paste-' + Date.now() + guessExt(f.type));
      const r = await fetch('/api/upload?name=' + encodeURIComponent(name), {
        method: 'PUT',
        headers: {'Content-Type': f.type || 'application/octet-stream'},
        body: f
      });
      if (!r.ok) {
        let msg = 'HTTP ' + r.status;
        try { msg = (await r.json()).error || msg; } catch(e){}
        failMsg = f.name + '：' + msg;
      } else {
        okCount++;
      }
    } catch(e) {
      failMsg = f.name + '：' + e.message;
    } finally {
      doneCount++;
      bar.querySelector('i').style.width = Math.round(doneCount / busy * 100) + '%';
    }
  }

  bar.querySelector('i').style.width = '100%';
  setTimeout(() => { bar.classList.remove('on'); bar.querySelector('i').style.width = '0%'; }, 400);
  busy = 0;

  await refresh();
  if (okCount && !failMsg) toast('上传成功 ' + okCount + ' 张，可点「复制链接」');
  else if (okCount) toast('成功 ' + okCount + ' 张，失败：' + failMsg, true);
  else if (failMsg) toast('上传失败：' + failMsg, true);
}

function guessExt(type){
  const m = {'image/png':'.png','image/jpeg':'.jpg','image/gif':'.gif',
             'image/webp':'.webp','image/bmp':'.bmp','image/svg+xml':'.svg'};
  return m[type] || '.png';
}

/* ---------- 交互绑定 ---------- */
const drop = $('#drop');
const fileInput = $('#file');

drop.addEventListener('click', () => fileInput.click());
fileInput.addEventListener('change', () => { uploadFiles(fileInput.files); fileInput.value = ''; });

['dragenter','dragover'].forEach(ev => {
  drop.addEventListener(ev, e => { e.preventDefault(); drop.classList.add('hot'); });
});
['dragleave','dragend'].forEach(ev => {
  drop.addEventListener(ev, e => { e.preventDefault(); drop.classList.remove('hot'); });
});
drop.addEventListener('drop', e => {
  e.preventDefault();
  drop.classList.remove('hot');
  if (e.dataTransfer && e.dataTransfer.files) uploadFiles(e.dataTransfer.files);
});
/* 整页都接受拖放, 但别让浏览器打开文件 */
window.addEventListener('dragover', e => e.preventDefault());
window.addEventListener('drop', e => e.preventDefault());

/* 粘贴截图 */
window.addEventListener('paste', e => {
  const items = (e.clipboardData && e.clipboardData.items) || [];
  const files = [];
  for (const it of items) {
    if (it.kind === 'file') {
      const f = it.getAsFile();
      if (f) {
        const ext = guessExt(f.type);
        files.push(new File([f], 'paste-' + stamp() + ext, {type: f.type}));
      }
    }
  }
  if (files.length) { e.preventDefault(); uploadFiles(files); }
});

function stamp(){
  const d = new Date(), p = n => String(n).padStart(2, '0');
  return d.getFullYear() + p(d.getMonth()+1) + p(d.getDate())
       + '-' + p(d.getHours()) + p(d.getMinutes()) + p(d.getSeconds());
}

$('#q').addEventListener('input', render);
$('#refresh').addEventListener('click', refresh);

/* 图片放大预览 */
const lb = $('#lb');
document.addEventListener('click', e => {
  const a = e.target.closest('a.thumb');
  if (a && e.target.tagName === 'IMG') {
    e.preventDefault();
    lb.querySelector('img').src = a.href;
    lb.classList.add('on');
  }
});
lb.addEventListener('click', () => lb.classList.remove('on'));
document.addEventListener('keydown', e => {
  if (e.key === 'Escape') lb.classList.remove('on');
});

/* ---------- 启动 ---------- */
$('#baseUrl').textContent = baseUrl() + '/';
refresh();
</script>
</body>
</html>
"""


# --------------------------------------------------------------------------
# HTTP 处理
# --------------------------------------------------------------------------
class Handler(BaseHTTPRequestHandler):
    server_version = "imghost/" + VERSION
    protocol_version = "HTTP/1.1"

    # 安静一点, 只打有意义的信息
    def log_message(self, fmt, *args):
        pass

    # ---------------- 小工具 ----------------
    def _json(self, obj, code=200):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _err(self, code, msg):
        self._json({"error": msg}, code)

    def _spa(self):
        """拆分路径与 query。"""
        raw = urllib.parse.urlsplit(self.path)
        path = urllib.parse.unquote(raw.path)
        qs = urllib.parse.parse_qs(raw.query)
        return path, qs

    # ---------------- CORS 预检 ----------------
    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, PUT, DELETE, HEAD, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Access-Control-Max-Age", "86400")
        self.send_header("Content-Length", "0")
        self.end_headers()

    # ---------------- GET ----------------
    def do_HEAD(self):
        self._serve(target_only=True)

    def do_GET(self):
        path, qs = self._spa()

        if path == "/" or path == "/index.html":
            return self._send_page()
        if path == "/api/list":
            return self._api_list()
        if path in ("/favicon.ico", "/robots.txt"):
            self.send_response(204)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if path.startswith("/api/"):
            return self._err(404, "未知接口")
        return self._serve()

    def _send_page(self):
        base = CFG.base_url
        html = (PAGE
                .replace("__TITLE__", html_escape(CFG.title))
                .replace("__BASE_URL__", base)
                .replace("__ROOT__", html_escape(CFG.root))
                .replace("__READONLY__", "true" if CFG.readonly else "false")
                .replace("__MAX_MB__", str(int(CFG.max_bytes / 1024 / 1024)))
                ).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(html)))
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        try:
            self.wfile.write(html)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _api_list(self):
        items = []
        try:
            with os.scandir(CFG.root) as it:
                for e in it:
                    if not e.is_file() or e.name.startswith("."):
                        continue
                    try:
                        st = e.stat()
                    except OSError:
                        continue
                    items.append({
                        "name": e.name,
                        "size": st.st_size,
                        "time": time.strftime("%Y-%m-%d %H:%M", time.localtime(st.st_mtime)),
                        "mtime": st.st_mtime,
                        "isImage": (mimetypes.guess_type(e.name)[0] or "").startswith("image/"),
                    })
        except FileNotFoundError:
            pass
        items.sort(key=lambda x: x["mtime"], reverse=True)
        self._json({"items": items, "count": len(items)})

    def _serve(self, target_only=False):
        path, qs = self._spa()
        name = safe_name(path.lstrip("/"))
        full = os.path.join(CFG.root, name)

        if not name or name.startswith(".") or not os.path.isfile(full):
            if target_only:
                self.send_response(404)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            return self._not_found()

        try:
            f = open(full, "rb")
        except OSError:
            return self._not_found()

        f.seek(0, os.SEEK_END)
        size = f.tell()
        f.seek(0)

        # 单段 Range 支持 (大视频/PDF 预览需要)
        start, end, partial = 0, size - 1, False
        rng = self.headers.get("Range", "")
        m = re.match(r"bytes=(\d*)-(\d*)$", rng.strip())
        if m and size > 0:
            if m.group(1) == "" and m.group(2):
                start = max(0, size - int(m.group(2)))
            elif m.group(1):
                start = int(m.group(1))
                if m.group(2):
                    end = min(size - 1, int(m.group(2)))
            if start <= end and start < size:
                partial = True
            else:
                start, end, partial = 0, size - 1, False

        length = end - start + 1
        ctype = mimetypes.guess_type(name)[0] or "application/octet-stream"

        self.send_response(206 if partial else 200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(length))
        self.send_header("Accept-Ranges", "bytes")
        if partial:
            self.send_header("Content-Range", "bytes %d-%d/%d" % (start, end, size))
        # 关键: inline 才能让 <img> 直接显示, 而不是弹下载框
        self.send_header("Content-Disposition",
                         "inline; filename*=UTF-8''%s" % urllib.parse.quote(name))
        self.send_header("Cache-Control", "public, max-age=86400")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()

        if target_only:
            f.close()
            return

        try:
            if start:
                f.seek(start)
            remaining = length
            while remaining > 0:
                chunk = f.read(min(256 * 1024, remaining))
                if not chunk:
                    break
                self.wfile.write(chunk)
                remaining -= len(chunk)
        except (BrokenPipeError, ConnectionResetError):
            pass
        finally:
            f.close()

    def _not_found(self):
        body = ("<!DOCTYPE html><meta charset=utf-8>"
                "<body style='background:#0f1115;color:#98a1b3;font-family:sans-serif;"
                "display:flex;align-items:center;justify-content:center;height:100vh;margin:0'>"
                "<div style='text-align:center'><h2 style='color:#e6e9ef'>404</h2>"
                "<p>这张图片不存在或已被删除。</p>"
                "<p><a style='color:#4c8dff' href='/'>&larr; 回到图床首页</a></p></div>"
                ).encode("utf-8")
        self.send_response(404)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    # ---------------- 上传 ----------------
    def do_PUT(self):
        path, qs = self._spa()
        if path != "/api/upload":
            return self._err(404, "未知接口，上传请用 PUT /api/upload?name=文件名")
        if CFG.readonly:
            return self._err(403, "服务器已设置为只读")

        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            return self._err(400, "Content-Length 无效")
        if length <= 0:
            return self._err(400, "空文件")
        if length > CFG.max_bytes:
            return self._err(413, "文件超过 %d MB 上限" % int(CFG.max_bytes / 1024 / 1024))

        raw_name = (qs.get("name") or [""])[0]
        base = safe_name(raw_name or ("upload-" + uuid.uuid4().hex[:8] + ".bin"))

        try:
            name, dest = reserve_unique(CFG.root, base)
        except OSError as exc:
            return self._err(500, "无法分配文件名：%s" % exc)
        tmp = os.path.join(CFG.root, ".part-" + uuid.uuid4().hex)

        try:
            remaining = length
            with open(tmp, "wb") as f:
                while remaining > 0:
                    chunk = self.rfile.read(min(1024 * 1024, remaining))
                    if not chunk:
                        raise IOError("连接中断，上传不完整")
                    f.write(chunk)
                    remaining -= len(chunk)
            os.replace(tmp, dest)
        except Exception as exc:
            try:
                if os.path.exists(tmp):
                    os.remove(tmp)
            except OSError:
                pass
            # 上传失败要把占位的空文件删掉, 否则会留下 0 字节图片
            try:
                if os.path.exists(dest) and os.path.getsize(dest) == 0:
                    os.remove(dest)
            except OSError:
                pass
            return self._err(500, "保存失败：%s" % exc)

        st = os.stat(dest)
        self._json({
            "ok": True,
            "name": name,
            "size": st.st_size,
            "url": CFG.base_url + "/" + urllib.parse.quote(name),
            "path": "/" + urllib.parse.quote(name),
        })
        print("  上传  %-40s %s" % (name, human_size(st.st_size)))

    # ---------------- 删除 ----------------
    def do_DELETE(self):
        path, qs = self._spa()
        if path != "/api/delete":
            return self._err(404, "未知接口")
        if CFG.readonly:
            return self._err(403, "服务器已设置为只读")

        raw_name = (qs.get("name") or [""])[0]
        name = safe_name(raw_name)
        if not name or name != raw_name.strip() or name.startswith("."):
            return self._err(400, "文件名不合法")
        full = os.path.join(CFG.root, name)
        if not os.path.isfile(full):
            return self._err(404, "文件不存在")
        try:
            os.remove(full)
        except OSError as exc:
            return self._err(500, "删除失败：%s" % exc)
        print("  删除  %s" % name)
        self._json({"ok": True})


def html_escape(s):
    return (s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            .replace('"', "&quot;"))


# --------------------------------------------------------------------------
# 启动
# --------------------------------------------------------------------------
BANNER = r"""
  ╭──────────────────────────────────────────────────────────────╮
  │  🖼️  简易图床 imghost v{ver}   —— 零依赖 / 免登录 / 拖图即用    │
  ╰──────────────────────────────────────────────────────────────╯

    本机打开 :  {local}
    局域网   :  {lan}
    图片目录 :  {root}
    直链格式 :  {base}/<文件名>

    把图片拖到网页里 → 点「复制链接」→ 粘到文档 / 网页 / 聊天窗口
    按 Ctrl+C 停止服务
"""


def main():
    global CFG

    p = argparse.ArgumentParser(
        description="简易图床：单文件、零依赖、免登录，拖图即得直链。")
    p.add_argument("--port", "-p", type=int, default=8899,
                   help="监听端口，默认 8899")
    p.add_argument("--bind", "-b", default="0.0.0.0",
                   help="监听地址，默认 0.0.0.0（局域网可访问）")
    p.add_argument("--dir", "-d", default="images", help="图片存放目录，默认 ./images")
    p.add_argument("--max-mb", type=float, default=50, help="单文件大小上限(MB)，默认 50")
    p.add_argument("--cdn-base", default="",
                   help="直链前缀。配相对路径（如 /imgs）可让图片与页面同源")
    p.add_argument("--title", default="内部图床", help="页面标题")
    p.add_argument("--readonly", action="store_true", help="只读模式：禁止上传和删除")
    p.add_argument("--open", action="store_true", help="启动后自动打开浏览器")
    p.add_argument("--version", action="version", version="imghost " + VERSION)
    args = p.parse_args()

    root = os.path.abspath(args.dir)
    os.makedirs(root, exist_ok=True)

    ips = lan_ips()
    lan = ips[0] if ips else "127.0.0.1"

    CFG = Config(root=root, port=args.port, bind=args.bind, max_mb=args.max_mb,
                 cdn_base=args.cdn_base, title=args.title, readonly=args.readonly)

    host_part = args.bind if args.bind not in ("0.0.0.0", "::", "") else lan
    CFG.base_url = (args.cdn_base.rstrip("/") if args.cdn_base
                    else "http://%s:%d" % (host_part, args.port))

    # ---------------- 主监听 ----------------
    try:
        httpd = ThreadingHTTPServer((args.bind, args.port), Handler)
        httpd.daemon_threads = True
    except OSError as exc:
        print("\n  [错误] 无法监听 %s:%d —— %s" % (args.bind, args.port, exc))
        print("  端口可能已被别的程序占用，换个端口再试：")
        print("      python imghost.py --port 8900\n")
        sys.exit(1)

    # ---------------- 打印提示 ----------------
    print(BANNER.format(
        ver=VERSION,
        local="http://127.0.0.1:%d" % args.port,
        lan="http://%s:%d" % (lan, args.port),
        root=root,
        base=CFG.base_url,
    ), flush=True)

    if len(ips) > 1:
        print("    其他网卡 :  " + "  ".join("http://%s:%d" % (ip, args.port)
                                          for ip in ips[1:]) + "\n", flush=True)

    print("    提示：如果同一局域网的同事打不开，请在 Windows 防火墙放行端口：", flush=True)
    print("      netsh advfirewall firewall add rule name=\"imghost %d\" "
          "dir=in action=allow protocol=TCP localport=%d" % (args.port, args.port), flush=True)
    print("", flush=True)

    if args.open:
        threading.Timer(0.6, lambda: webbrowser.open(
            "http://127.0.0.1:%d" % args.port)).start()

    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n  已停止。图片都还在 %s\n" % root)
    finally:
        httpd.server_close()


if __name__ == "__main__":
    main()
