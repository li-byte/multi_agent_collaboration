"""离线自检：四个导出功能（消耗清单 PNG / 时序图 PNG+SVG / 数据流转 HTML / 会话记录 HTML）。

为什么需要它：这些导出**在离线自检里原本是隐形的** —— `check_js.py` 只做语法校验，
而"图能不能画出来""导出的 HTML 换台机器能不能打开"这类问题语法检查一概看不见。
偏偏它们又最难靠肉眼回归：PNG 得打开浏览器点按钮、HTML 得下载下来再打开。

做法（两步，都不需要浏览器）：
  1. **假 DOM 里真跑一遍**：把 `web/index.html` 的内联 JS 抠出来，与一份最小 DOM / Image /
     canvas / Blob 替身拼成同一个模块，直接调用 `buildUsageSvg` / `exportUsagePng` /
     `exportFlowHtml` / `exportConversationHtml` / `seqSvg`，断言产物内容、文件名与按钮文案；
     并把产物落盘。
  2. **把产物当数据校验**：PNG 的底图必须能被 XML 解析器解析（否则浏览器栅格化会中途失败）、
     且所有元素都在画布内（不溢出被裁）；导出的 HTML 必须**零外部引用**
     （没有 script/link/img/src/@import/url()/http 绝对地址），否则换台机器就打不开；
     会话记录那份还要确认正文里**没有**任何"要点击才成立"的控件（按钮 / 视图入口 / 等待动画）。

用法：
    python scripts/check_export.py        # 本机没有 node 时会跳过（与 check_js.py 一致）
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET
from pathlib import Path

HERE = Path(__file__).resolve().parent
WEB = HERE.parent / "web" / "index.html"
SCRIPT_RE = re.compile(r"<script(?![^>]*\bsrc=)[^>]*>(.*?)</script>", re.S | re.I)
STYLE_RE = re.compile(r"<style[^>]*>(.*?)</style>", re.S | re.I)
SVGNS = "{http://www.w3.org/2000/svg}"

# ---- 第一段：假 DOM（只提供导出代码真正用到的那几个口子） ----
SETUP = r"""
import fs from 'node:fs';
const HTML_PATH = process.argv[2], TMP = process.argv[3];
const __html = fs.readFileSync(HTML_PATH, 'utf8');
const __pageCss = __html.match(/<style[^>]*>([\s\S]*?)<\/style>/)[1];

const __els = {}, __anchors = [], __blobs = {};
let __blobSeq = 0;
function __el(tag) {
  return {
    tagName: tag, style: {}, dataset: {}, innerHTML: '', textContent: '', value: '',
    onclick: null, scrollTop: 0, scrollHeight: 0, clientHeight: 0, clientWidth: 1200,
    classList: { add() {}, remove() {}, contains() { return false; } },
    addEventListener() {}, focus() {}, remove() {}, appendChild() {},
    click() { if (this.tagName === 'a') __anchors.push(this); },
    querySelectorAll() { return []; }, querySelector() { return null; },
    getAttribute() { return null; }, setAttribute() {}, cloneNode() { return __el(tag); },
    getContext() { return { fillStyle: '', fillRect() {}, drawImage() {}, setTransform() {} }; },
    toBlob(cb) { cb(new Blob(['fake-png'], { type: 'image/png' })); },
  };
}
const __fakeSvg = {
  attrs: { viewBox: '0 0 1240 640' }, clientWidth: 1240, clientHeight: 640,
  getAttribute(k) { return this.attrs[k] || null; },
  setAttribute(k, v) { this.attrs[k] = v; },
  cloneNode() { const c = Object.create(__fakeSvg); c.attrs = Object.assign({}, this.attrs); return c; },
};
const __seqCanvas = __el('div');
__seqCanvas.querySelector = (s) => (s === 'svg' ? __fakeSvg : null);

global.document = {
  getElementById: (id) => (id === 'seq-canvas' ? __seqCanvas
                                              : (__els[id] = __els[id] || __el('div'))),
  body: __el('body'), addEventListener() {}, createElement: (t) => __el(t),
  querySelectorAll: (sel) => (sel === 'style' ? [{ textContent: __pageCss }] : []),
};
global.window = global;
global.requestAnimationFrame = (f) => f();
global.EventSource = class { constructor() {} close() {} addEventListener() {} };
global.fetch = async (url) => ({ ok: true,
  json: async () => (String(url).includes('/api/runs') ? global.__runsRows : {}) });
global.__runsRows = [];
global.alert = (m) => console.log('    [alert] ' + String(m).split('\n')[0]);
global.XMLSerializer = class { serializeToString(n) {
  return '<svg xmlns="http://www.w3.org/2000/svg" width="' + n.attrs.width +
         '" height="' + n.attrs.height + '" viewBox="' + n.attrs.viewBox +
         '"><text x="10" y="20">hi</text></svg>'; } };
global.URL.createObjectURL = (b) => { const u = 'blob:' + (++__blobSeq); __blobs[u] = b; return u; };
global.URL.revokeObjectURL = () => {};

const __results = [];
function __ok(cond, label, extra) {
  __results.push([!!cond, label]);
  console.log((cond ? '  ✓ ' : '  ✗ ') + label + (extra !== undefined && !cond ? '  → ' + extra : ''));
}
global.__TMP = TMP;
global.__anchors = __anchors;
global.__blobs = __blobs;
global.__ok = __ok;
global.__fs = fs;
global.__imgLoaded = 0;
global.__results = __results;
"""

# ---- 第三段：断言（紧跟在抠出来的内联 JS 后面，所以能直接用它声明的函数） ----
TESTS = r"""
/* ============ ① 消耗清单 → SVG ============ */
const __detail = {
  llm_enabled: true, llm_mode: 'deepseek', model: 'deepseek-chat',
  total: { calls: 6, prompt_tokens: 8462, completion_tokens: 1736, total_tokens: 10198,
           cached_tokens: 4096, reasoning_tokens: 0, duration_ms: 10904, failed: 0 },
  by_role: [
    { role:'reviewer',  calls:1, prompt_tokens:2261, completion_tokens:438, total_tokens:2699, duration_ms:2250 },
    { role:'generator', calls:2, prompt_tokens:2127, completion_tokens:367, total_tokens:2494, duration_ms:2562 },
    { role:'executor',  calls:1, prompt_tokens:780,  completion_tokens:95,  total_tokens:875,  duration_ms:703 },
  ],
  by_stage: [
    { role:'generator', stage:'第一层·选表', calls:1, total_tokens:800 },
    { role:'generator', stage:'第二层·生成 SQL', calls:1, total_tokens:1694 },
  ],
  calls: [
    { call_id:1, role:'planner', stage:'理解问题并拆任务', round:0, cursor:0, attempt:1,
      model:'deepseek-flash', prompt_tokens:1731, completion_tokens:267, total_tokens:1998,
      cached_tokens:1536, duration_ms:2764, ok:true },
    { call_id:3, role:'generator', stage:'第二层·生成 SQL', round:0, cursor:0, attempt:2,
      model:'deepseek-flash', prompt_tokens:1441, completion_tokens:253, total_tokens:1694,
      cached_tokens:512, duration_ms:1437, ok:false,
      error:'契约校验失败 <字段 nickname> & 已重试' },
  ],
};
store.runId = 'r1'; store.infoMode = 'usage'; store.usageDetail = __detail;
const __svg = buildUsageSvg(__detail);
__fs.writeFileSync(__TMP + '/export_usage.svg', __svg.svg, 'utf8');
const __must = ['大模型消耗清单','10,198','8,462','4,096','48%','按智能体','按步骤','调用明细',
                '汇总审查','第一层·选表','deepseek-flash','第 2 次重试'];
const __miss = __must.filter((s) => !__svg.svg.includes(s));
__ok(!__miss.length, '消耗清单 SVG 内容完整', JSON.stringify(__miss));
__ok(!__svg.svg.includes('全部会话累计'), 'SVG 里没有「全部会话累计」');
__ok(__svg.svg.includes('&lt;字段 nickname&gt; &amp; 已重试'),
     '失败调用的错误原文完整保留（列宽为它让路，不截断）');
__ok(__svg.svg.startsWith('<svg xmlns="http://www.w3.org/2000/svg"') && __svg.svg.endsWith('</svg>'),
     'SVG 首尾正确');
__ok(__svg.svg.includes('width="' + __svg.w + '" height="' + __svg.h + '"'),
     'width/height 与返回值一致');
__ok((__svg.svg.match(/<text/g) || []).length === (__svg.svg.match(/<\/text>/g) || []).length,
     '<text> 标签成对');
__ok(!__svg.svg.match(/&(?!(amp|lt|gt|quot|#\d+);)/g), '文本里的 & < > 都转义了');

/* 0 次调用：mock 与「老记录」两种文案都要能出图 */
store.usageDetail = { llm_enabled:false, llm_mode:'mock', model:'deepseek-chat',
                      total:{}, by_role:[], by_stage:[], calls:[] };
__ok(buildUsageSvg(store.usageDetail).svg.includes('根本没发请求'),
     'mock 且无记录：如实写「根本没发请求」');
store.usageDetail = { llm_enabled:true, llm_mode:'deepseek', model:'deepseek-chat',
                      total:{calls:0}, by_role:[], by_stage:[], calls:[] };
__ok(buildUsageSvg(store.usageDetail).svg.includes('上线之前'),
     '有 run 但无 llm_call 行：说明可能是老数据');

/* ============ ② 消耗清单 → PNG（Image → canvas → toBlob） ============ */
global.Image = class {
  set src(v) { __imgLoaded++; this._src = v; setTimeout(() => this.onload && this.onload(), 0); }
  get src() { return this._src; }
};
__anchors.length = 0;
store.usageDetail = __detail;
await exportUsagePng();
await new Promise((r) => setTimeout(r, 5));
__ok(__imgLoaded === 1, 'PNG 导出真的把 SVG 交给了 Image');
__ok(__anchors.length === 1 && __anchors[0].download === '大模型消耗清单.png', 'PNG 文件名正确');
__ok(__anchors.length === 1 && __blobs[__anchors[0].href]
     && __blobs[__anchors[0].href].type === 'image/png', '产出的是 image/png');

/* ============ ③ 时序图 → PNG / SVG ============ */
const __seq = seqSvg();
__ok(__seq && __seq.w === 1240 && __seq.h === 640, '时序图尺寸取自 viewBox');
__ok(__seq && __seq.text.includes('width="1240"') && __seq.text.includes('height="640"'),
     '序列化时补上了 width/height（否则部分浏览器画不出图）');
__anchors.length = 0;
await exportSequencePng();
await new Promise((r) => setTimeout(r, 5));
__ok(__anchors.length === 1 && __anchors[0].download === '协作时序图.png', '时序图 PNG 文件名正确');
__anchors.length = 0;
exportSequenceSvg();
__ok(__anchors.length === 1 && __anchors[0].download === '协作时序图.svg',
     '时序图 SVG 仍可导出（矢量版）');

/* PNG 画不出来时要回退到 SVG，不能静默失败 */
const __ORIG_Image = global.Image;
global.Image = class { set src(v) { setTimeout(() => this.onerror && this.onerror(), 0); } };
__anchors.length = 0;
await exportSequencePng();
__ok(__anchors.length === 1 && __anchors[0].download === '协作时序图.svg',
     'canvas 画不出来时自动回退导出 SVG');
global.Image = __ORIG_Image;

/* ============ ④ 数据流转 → 自包含 HTML ============ */
store.events = [
  { seq:1, role:'system', event_type:'run_created', round:0, version:1,
    payload:{ question:'先找出库存小于 10 的商品 <测试> & 复查',
              tables:['customers','order_items','orders','products'] } },
  { seq:2, role:'planner', event_type:'node_start', round:0, version:1, payload:{} },
  { seq:3, role:'planner', event_type:'plan', round:0, version:1,
    payload:{ phase:'initial', understanding:'先查库存', intents:[
      { sub_task_id:'st-1', kind:'查询', intent:'查库存小于 10 的商品',
        depends_on:[], acceptance:'拿到商品 id' }] } },
  { seq:4, role:'planner', event_type:'handoff', round:0, version:1,
    payload:{ from:'planner', to:'generator', reason:'为子任务 st-1 生成 SQL', overridden:false } },
  { seq:5, role:'generator', event_type:'node_start', round:0, version:1, payload:{} },
  { seq:6, role:'generator', event_type:'sql', round:0, version:1,
    payload:{ sql:'SELECT * FROM products WHERE stock < 10',
              verdict:{ level:'只读', action:'执行', notes:['没有 LIMIT'], reasons:[] } } },
  { seq:7, role:'generator', event_type:'handoff', round:0, version:1,
    payload:{ from:'generator', to:'executor', reason:'生成完毕',
              overridden:true, model_wanted:'reviewer' } },
  { seq:8, role:'generator', event_type:'state_delta', round:0, version:1,
    payload:{ keys:['sql','checks','next_agent'],
              delta:{ sql:'SELECT * FROM products WHERE stock < 10' } } },
  { seq:9, role:'executor', event_type:'memory', round:0, version:2,
    payload:{ sql_id:44, rowcount:10, claim:'查库存小于 10 的商品 —— 查询返回 10 行',
              entities:{ table:'products', key:'id', values:[1,3,5] } } },
  { seq:10, role:'reviewer', event_type:'review', round:0, version:3,
    payload:{ decision:'approve', final_answer:'| id |' } },
];
store.chat = [{ kind:'user', text:'先找出库存小于 10 的商品' }];
store.rawAll = false;
__anchors.length = 0;
exportFlowHtml();
__ok(__anchors.length === 1 && __anchors[0].download === '数据流转链路.html', 'HTML 文件名正确');
__ok(store.rawAll === false, '导出没有改掉界面上「展开/收起」的状态');
const __flowHtml = __anchors.length ? await __blobs[__anchors[0].href].text() : '';
__fs.writeFileSync(__TMP + '/export_flow.html', __flowHtml, 'utf8');
__ok(__flowHtml.startsWith('<!doctype html>'), '是完整 HTML 文档');
__ok(!/<script/i.test(__flowHtml), '没有 <script>');
__ok(__flowHtml.includes('class="flow"') && __flowHtml.includes('hop-bd'), '流转卡片渲染出来了');
__ok(__flowHtml.includes('真实传递过去的内容'), '带上「真实传递过去的内容·完整结构」');
__ok(__flowHtml.includes('<details') && __flowHtml.includes(' open>'),
     '原始结构强制展开（静态页里没有那个开关）');
__ok(__flowHtml.includes('护栏改道') && __flowHtml.includes('模型建议交给'),
     '护栏改道写清了「模型原本想给谁、护栏改去哪」');
__ok(__flowHtml.includes('&lt;测试&gt; &amp; 复查'), '问题里的 < & 被转义，不破页');
__ok(__flowHtml.includes('.hop') && __flowHtml.includes('--planner'), '页面样式已内联');

/* ============ ⑤ 会话记录 → 自包含 HTML ============ */
/* 造一段两轮的对话：第一轮正常跑完、第二轮停在等确认 + 一个还在进行中的气泡。
   直接摆 store.chat 的条目（就是渲染真正读的那个结构），覆盖每一类分支。 */
store.conversationId = 'conv-1';
store.rawAll = false;
store.chat = [
  { kind:'user', text:'查一下北京客户的订单总额 <带标签> & 符号' },
  { kind:'agent', role:'planner', round:0, typing:false,
    items:[{ type:'plan', understanding:'查订单总额',
             intents:[{ sub_task_id:'st-1', kind:'查询', intent:'查订单总额',
                        depends_on:[], acceptance:'拿到金额' }] }] },
  { kind:'agent', role:'executor', round:0, typing:false,
    items:[{ type:'memory', claim:'查订单总额 —— 查询返回 8 行',
             sql_id:44, rowcount:8, entities:{ table:'customers', key:'id', values:[1,2] } }] },
  { kind:'turn' },
  { kind:'user', text:'删掉所有已取消的订单' },
  { kind:'agent', role:'executor', round:0, typing:false,
    items:[{ type:'confirm', done:false, req:{ sql:"DELETE FROM orders WHERE status='已取消'",
             risk_level:'需确认', action:'DELETE', tables:['orders'], est_rows:3,
             cascade:['orders 的删除会级联删除 order_items.order_id'] } }] },
  { kind:'agent', role:'reviewer', round:0, typing:true, items:[] },
  { kind:'sys', done:true, text:'已完成 · 共 2 轮' },
];
global.__runsRows = [{ conv_id:'conv-1', question:'查一下北京客户的订单总额', status:'done',
                       turns:2, reads:1, writes:1, affected:4, write_tables:['orders'],
                       llm_calls:6, llm_tokens:10198 }];

/* 先确认「界面上」该有的交互件还在（后面导出要去掉它们，两者必须不同） */
const __live = renderChatHtml();
__ok(__live.includes('data-confirm') && __live.includes('data-open'),
     '界面上确认按钮与视图入口照常渲染（导出开关没有污染它）');

__anchors.length = 0;
await exportConversationHtml();
__ok(__anchors.length === 1
     && __anchors[0].download === '会话记录-查一下北京客户的订单总额 带标签 & 符号.html',
     '会话 HTML 文件名取自首问（< > : / 这类非法字符已换成空格）',
     __anchors.length ? __anchors[0].download : '没下载');
const __conv = __anchors.length ? await __blobs[__anchors[0].href].text() : '';
__fs.writeFileSync(__TMP + '/export_conversation.html', __conv, 'utf8');
/* 只看 </style> 之后的正文：内联的 CSS 里本来就有 [data-confirm] 这条选择器 */
const __convBody = __conv.slice(__conv.indexOf('</style>'));
__ok(__conv.startsWith('<!doctype html>'), '会话：是完整 HTML 文档');
__ok(!/<script/i.test(__conv) && !/\ssrc\s*=/.test(__conv) && !/https?:\/\//.test(__conv),
     '会话：零外部引用（没有 script / src= / http 地址）');
__ok(__conv.includes('会话记录') && __conv.includes('📄'), '会话：页眉有标题');
__ok(__conv.includes('2 轮对话') && __conv.includes('改 orders 4 行') && __conv.includes('10,198 tok'),
     '会话：页眉汇总行写了轮次 / 改了什么数据 / 消耗');
__ok(!__convBody.includes('data-confirm') && !__convBody.includes('data-open'),
     '会话：导出的是记录，不是界面（正文里没有按钮、没有视图入口）');
__ok(__conv.includes('⏸ 当时在等用户确认'), '会话：待确认那一步如实写成"当时在等确认"');
__ok(!__conv.includes('class="dots"') && __conv.includes('（导出时这一步还在进行中'),
     '会话：没跑完的气泡不留空白，写明当时状态');
__ok(__conv.includes('新的一轮'), '会话：多轮之间的分隔保留');
__ok(__conv.includes('&lt;带标签&gt; &amp; 符号'), '会话：用户输入已转义');
__ok(store.exporting === false, '会话：导出结束把 exporting 复位（界面行为不受影响）');
__ok(renderChatHtml().includes('data-confirm'), '会话：导出之后界面渲染恢复原样');

/* 没有对话时不能导出（点了只给一句提示，不能下载一个空壳） */
const __savedChat = store.chat;
store.chat = [];
__anchors.length = 0;
await exportConversationHtml();
__ok(__anchors.length === 0, '没有对话时不产生下载');
store.chat = __savedChat;

/* ============ ⑥ 按钮随视图切换 ============ */
await openInfo('usage');
__ok(document.getElementById('info-export').textContent === '导出 PNG'
     && document.getElementById('info-export').style.display === ''
     && document.getElementById('info-export2').style.display === 'none',
     '消耗清单只有「导出 PNG」');
await openInfo('seq');
__ok(document.getElementById('info-export2').textContent === '导出 SVG'
     && document.getElementById('info-export2').style.display === '',
     '时序图是「导出 PNG」+「导出 SVG」');
await openInfo('flow');
__ok(document.getElementById('info-export').textContent === '导出 HTML'
     && document.getElementById('info-export2').style.display === 'none',
     '数据流转是「导出 HTML」');
await openInfo('audit');
__ok(document.getElementById('info-export').style.display === 'none'
     && document.getElementById('info-export2').style.display === 'none',
     'SQL 审计没有导出按钮');

const __bad = __results.filter((r) => !r[0]).length;
console.log('JSON ' + JSON.stringify({ total: __results.length, bad: __bad }));
"""


def est_w(text: str, size: float) -> float:
    """与前端 tw() 同一套估宽规则：CJK 算 1 em、其余算 0.55 em。"""
    return sum(size if ord(c) > 0x2E80 else size * 0.55 for c in text)


def check_svg(path: Path, out: list[tuple[bool, str]]) -> None:
    raw = path.read_text(encoding="utf-8")
    try:
        root = ET.fromstring(raw)
    except ET.ParseError as exc:
        out.append((False, f"PNG 底图是合法 XML（{exc}）"))
        return
    out.append((True, "PNG 底图是合法 XML（浏览器栅格化不会中途失败）"))
    out.append((root.tag == SVGNS + "svg", "根节点是 svg"))
    w, h = float(root.get("width")), float(root.get("height"))
    out.append((w > 800 and h > 300, f"画布尺寸合理（{w:.0f}×{h:.0f}）"))
    out.append((root.get("viewBox") == f"0 0 {w:.0f} {h:.0f}", "viewBox 与 width/height 一致"))

    def walk(node, dx: float = 0.0, dy: float = 0.0) -> list[str]:
        ox, oy = dx, dy
        tr = node.get("transform")
        if tr:
            m = re.match(r"\s*translate\(\s*([-\d.]+)[ ,]+([-\d.]+)\s*\)", tr)
            if m:
                ox, oy = ox + float(m.group(1)), oy + float(m.group(2))
        bad: list[str] = []
        if node.tag == SVGNS + "rect" and node.get("width"):
            x, y = ox + float(node.get("x") or 0), oy + float(node.get("y") or 0)
            rw, rh = float(node.get("width")), float(node.get("height"))
            if x < -0.5 or y < -0.5 or x + rw > w + 0.5 or y + rh > h + 0.5:
                bad.append(f"rect({x:.0f},{y:.0f},{rw:.0f}×{rh:.0f})")
        if node.tag == SVGNS + "text":
            size = float(node.get("font-size") or 12)
            x, y = ox + float(node.get("x") or 0), oy + float(node.get("y") or 0)
            tw = est_w(node.text or "", size)
            x0, x1 = ((x - tw, x) if (node.get("text-anchor") or "start") == "end" else (x, x + tw))
            if x0 < -0.5 or x1 > w + 0.5 or y < 0 or y > h:
                bad.append(f"text({(node.text or '')[:18]!r})")
        for child in node:
            bad += walk(child, ox, oy)
        return bad

    over = walk(root)
    out.append((not over, f"没有元素画到画布外（{len(over)} 处溢出）"))


def check_html(path: Path, out: list[tuple[bool, str]]) -> None:
    html = path.read_text(encoding="utf-8")
    out.append((html.lstrip().startswith("<!doctype html>"), "是完整 HTML 文档"))
    out.append((html.count("<style") == 1, "恰好内联了一份样式表"))
    lower = html.lower()
    for token, label in [("<script", "没有 <script>"), ("<link", "没有 <link>"),
                         ("<img", "没有 <img>"), ("<iframe", "没有 <iframe>"),
                         ("@import", "没有 @import")]:
        out.append((token not in lower, label))
    for pat, label in [(r"\ssrc\s*=", "没有 src= 引用"),
                       (r"url\s*\(", "没有 url(...) 外部资源"),
                       (r"https?://", "没有任何 http(s) 绝对地址"),
                       (r"/api/", "没有回连本站接口")]:
        hits = re.findall(pat, html)
        out.append((not hits, label))
    out.append((html.rstrip().endswith("</html>"), "文档正常收尾"))


def main() -> int:
    if not WEB.exists():
        print(f"找不到 {WEB}")
        return 1
    node = shutil.which("node")
    if not node:
        print("跳过：本机没有 node，无法在假 DOM 里跑导出代码")
        return 0

    html_src = WEB.read_text(encoding="utf-8")
    blocks = SCRIPT_RE.findall(html_src)
    if not blocks:
        print("web/index.html 里没有内联 <script> 块")
        return 1
    code = blocks[0]

    print("假 DOM 里跑一遍四个导出（消耗清单 PNG · 时序图 PNG/SVG · 数据流转 HTML · 会话记录 HTML）：")
    with tempfile.TemporaryDirectory() as td:
        harness = Path(td) / "harness.mjs"
        # 三段拼成一个模块：假 DOM + 抠出来的内联 JS + 断言。
        # 直接拼而不是 new Function(...)：内联 JS 里有 `\d`、`${}` 这类写法，
        # 塞进字符串或模板字面量会被转义规则悄悄改掉（正则就成了另一个正则）。
        harness.write_text(SETUP + "\n" + code + "\n" + TESTS, encoding="utf-8")
        proc = subprocess.run([node, str(harness), str(WEB), td],
                              capture_output=True, text=True, encoding="utf-8", errors="replace")
        print(proc.stdout.rstrip())
        if proc.returncode != 0:
            if proc.stderr:
                print(proc.stderr.rstrip()[:2000])
            print("\n✗ 导出代码在假 DOM 里执行失败")
            return 1
        found = re.search(r"JSON (\{.*\})", proc.stdout)
        if not found:
            print("\n✗ 没拿到断言统计")
            return 1
        stat = json.loads(found.group(1))
        total, bad = stat["total"], stat["bad"]

        print("\n产物校验（把导出结果当数据来验，不看截图）：")
        extra: list[tuple[bool, str]] = []
        svg_file = Path(td) / "export_usage.svg"
        if svg_file.exists():
            check_svg(svg_file, extra)
        else:
            extra.append((False, "生成了 export_usage.svg"))
        for name, label in [("export_flow.html", "数据流转"), ("export_conversation.html", "会话记录")]:
            page = Path(td) / name
            before = len(extra)
            if page.exists():
                check_html(page, extra)
                # 名字带上这段，免得两条 20 项混在一起看不出是谁的
                extra[before:] = [(good, f"{label}：{text}") for good, text in extra[before:]]
            else:
                extra.append((False, f"生成了 {name}"))
        for good, label in extra:
            print(("  ✓ " if good else "  ✗ ") + label)
        total += len(extra)
        bad += sum(1 for good, _ in extra if not good)

    print(f"\n共 {total} 项，失败 {bad} 项。")
    if bad:
        return 1
    print("✓ 导出符合预期：图能画出来、元素不溢出、HTML 零外部依赖（换台机器也能打开）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
