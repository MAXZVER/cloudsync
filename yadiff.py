#!/usr/bin/env python3
"""Разрешение конфликтов: три панели, как в IDE.

Слева — версия с Яндекс.Диска, справа — версия с этого Mac, посередине живой
результат. Шевроны в жёлобах добавляют сторону в результат; порядок нажатия
задаёт порядок строк, поэтому «сначала левое, потом правое» и наоборот
получаются без отдельных кнопок.

Зависимостей нет — только стандартная библиотека.

Офисные файлы сравниваются по тексту:
  .docx .doc .rtf .odt   — через textutil, он встроен в macOS
  .xlsx .pptx            — zip + XML разбираются стандартной библиотекой
Собрать .docx обратно из текста нельзя, поэтому для них доступен только выбор
версии целиком.

Про автослияние. Двух версий для него мало: строка есть слева и нет справа —
это либо «слева добавили», либо «справа удалили», и дифф эти случаи не
различает. Нужна третья версия — общий предок. Его сохраняет yasync.py после
каждой удачной синхронизации, и тогда работает build_blocks3: блок, который
изменила ровно одна сторона, сливается автоматически и правильно, включая
удаления. Если предка нет (файл появился уже в конфликте, слишком большой,
снимок потерян), остаётся догадка по пустой стороне — только вставки.

Времени правки отдельных строк нигде нет: у файла есть только общий mtime,
он показан в заголовке панели и говорит лишь, какой файл сохранён позже.
"""
import html
import json
import os
import re
import subprocess
import zipfile
import xml.etree.ElementTree as ET
from difflib import SequenceMatcher

TEXTUTIL_EXT = {".doc", ".docx", ".rtf", ".odt", ".rtfd", ".html", ".htm", ".webarchive"}
NS_MAIN = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
NS_DRAW = "{http://schemas.openxmlformats.org/drawingml/2006/main}"


# ------------------------------------------------------------------ чтение
def is_binary(path):
    try:
        with open(path, "rb") as f:
            return b"\0" in f.read(8192)
    except OSError:
        return True


def textutil(path):
    try:
        p = subprocess.run(["/usr/bin/textutil", "-convert", "txt", "-stdout", path],
                           capture_output=True, text=True, timeout=60)
        return p.stdout if p.returncode == 0 else None
    except (OSError, subprocess.SubprocessError):
        return None


def xlsx_text(path):
    """Листы -> строки вида 'Лист!A1: значение'."""
    try:
        z = zipfile.ZipFile(path)
    except (zipfile.BadZipFile, OSError):
        return None
    shared = []
    if "xl/sharedStrings.xml" in z.namelist():
        for si in ET.fromstring(z.read("xl/sharedStrings.xml")):
            shared.append("".join(t.text or "" for t in si.iter(NS_MAIN + "t")))
    names = {}
    if "xl/workbook.xml" in z.namelist():
        wb = ET.fromstring(z.read("xl/workbook.xml"))
        for i, sh in enumerate(wb.iter(NS_MAIN + "sheet"), start=1):
            names[i] = sh.get("name", "Лист%d" % i)
    out = []
    for n in sorted(x for x in z.namelist() if re.match(r"xl/worksheets/sheet\d+\.xml$", x)):
        idx = int(re.search(r"sheet(\d+)\.xml", n).group(1))
        title = names.get(idx, "Лист%d" % idx)
        for c in ET.fromstring(z.read(n)).iter(NS_MAIN + "c"):
            v = c.find(NS_MAIN + "v")
            if v is None or v.text is None:
                continue
            val = (shared[int(v.text)] if c.get("t") == "s" and v.text.isdigit()
                   and int(v.text) < len(shared) else v.text)
            if val != "":
                out.append("%s!%s: %s" % (title, c.get("r", ""), val))
    return "\n".join(out)


def pptx_text(path):
    try:
        z = zipfile.ZipFile(path)
    except (zipfile.BadZipFile, OSError):
        return None
    out = []
    for n in sorted(x for x in z.namelist() if re.match(r"ppt/slides/slide\d+\.xml$", x)):
        out.append("--- слайд %s ---" % re.search(r"slide(\d+)\.xml", n).group(1))
        for t in ET.fromstring(z.read(n)).iter(NS_DRAW + "t"):
            if t.text:
                out.append(t.text)
    return "\n".join(out)


def to_lines(path):
    """(строки, можно_ли_сливать, чем_прочитано)."""
    ext = os.path.splitext(path)[1].lower()
    if ext in TEXTUTIL_EXT:
        t = textutil(path)
        return (t.splitlines() if t else None), False, "textutil"
    if ext == ".xlsx":
        t = xlsx_text(path)
        return (t.splitlines() if t else None), False, "разбор xlsx"
    if ext == ".pptx":
        t = pptx_text(path)
        return (t.splitlines() if t else None), False, "разбор pptx"
    if ext in (".xls", ".ppt"):
        return None, False, "старый двоичный формат"
    if is_binary(path):
        return None, False, "двоичный файл"
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            return f.read().splitlines(), True, "текст"
    except OSError:
        return None, False, "не прочитать"


# ------------------------------------------------------- посимвольный diff
WORD_RE = re.compile(r"\w+|\s+|[^\w\s]", re.UNICODE)


def inline_html(a, b):
    """Две строки -> HTML с выделенными участками, которые реально отличаются."""
    aw, bw = WORD_RE.findall(a), WORD_RE.findall(b)
    sm = SequenceMatcher(None, aw, bw, autojunk=False)
    ha, hb = [], []
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        ta = html.escape("".join(aw[i1:i2]))
        tb = html.escape("".join(bw[j1:j2]))
        if tag == "equal":
            ha.append(ta)
            hb.append(tb)
        else:
            if ta:
                ha.append('<em class="w">%s</em>' % ta)
            if tb:
                hb.append('<em class="w">%s</em>' % tb)
    return "".join(ha), "".join(hb)


def pair_lines(left, right):
    """Строки блока, разложенные парами. None — заполнитель."""
    rows = []
    sm = SequenceMatcher(None, left, right, autojunk=False)
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "equal":
            for k in range(i2 - i1):
                rows.append((left[i1 + k], right[j1 + k]))
        elif tag == "replace":
            n = max(i2 - i1, j2 - j1)
            for k in range(n):
                rows.append((left[i1 + k] if i1 + k < i2 else None,
                             right[j1 + k] if j1 + k < j2 else None))
        elif tag == "delete":
            for k in range(i1, i2):
                rows.append((left[k], None))
        else:
            for k in range(j1, j2):
                rows.append((None, right[k]))
    return rows


def _project(ops, p, end=False):
    """Позиция p в базовой версии -> позиция в стороне.

    Граница может попасть в стык: конец совпадающего куска и вставка нулевой
    ширины стоят в одной и той же точке базы. Поэтому для начала блока берём
    самую раннюю проекцию, для конца — самую позднюю: так вставка целиком
    попадает внутрь блока, а не теряется на его краю.
    """
    cand = []
    for tag, i1, i2, j1, j2 in ops:
        if not (i1 <= p <= i2):
            continue
        if tag == "equal":
            cand.append(j1 + (p - i1))
        else:
            if p == i1:
                cand.append(j1)
            if p == i2:
                cand.append(j2)
    if not cand:
        return ops[-1][4] if ops else 0
    return max(cand) if end else min(cand)


def _hunks(ops):
    """Участки базовой версии, которые сторона изменила. Вставка даёт i1 == i2."""
    return [(i1, i2) for tag, i1, i2, _, _ in ops if tag != "equal"]


def build_blocks3(base, a, b):
    """Трёхстороннее слияние: base — общий предок, a — слева, b — справа.

    Только имея предка, можно отличить «сторона добавила» от «другая удалила».
    Блок, который изменила ровно одна сторона, разрешается автоматически и
    однозначно — это и есть честное автослияние. Блок, который правили обе, —
    настоящий конфликт: auto = None, решает человек.
    """
    ops_a = SequenceMatcher(None, base, a, autojunk=False).get_opcodes()
    ops_b = SequenceMatcher(None, base, b, autojunk=False).get_opcodes()
    spans = sorted(_hunks(ops_a) + _hunks(ops_b))
    # Пересекающиеся и соприкасающиеся участки склеиваем в один блок.
    groups = []
    for s, e in spans:
        if groups and s <= groups[-1][1]:
            groups[-1][1] = max(groups[-1][1], e)
        else:
            groups.append([s, e])

    blocks = []
    cur = 0
    for s, e in groups:
        if s > cur:
            seg = base[cur:s]
            blocks.append({"tag": "same", "left": seg, "right": list(seg)})
        la = a[_project(ops_a, s):_project(ops_a, e, end=True)]
        lb = b[_project(ops_b, s):_project(ops_b, e, end=True)]
        bs = base[s:e]
        if la == lb:
            blocks.append({"tag": "same", "left": la, "right": list(lb)})
        else:
            auto = None
            if la == bs:
                auto = ["right"]          # тронули только справа
            elif lb == bs:
                auto = ["left"]           # тронули только слева
            blocks.append({"tag": "diff", "left": la, "right": lb,
                           "auto": auto, "base": bs})
        cur = e
    if cur < len(base):
        seg = base[cur:]
        blocks.append({"tag": "same", "left": seg, "right": list(seg)})
    return _finish(blocks)


def build_blocks(a, b, base=None):
    """Блоки для интерфейса. Соседние различия склеиваются.

    picks — стороны, включённые в результат, В ПОРЯДКЕ НАЖАТИЯ. Отсюда берётся
    «сначала левое, потом правое»: отдельных кнопок для порядка не нужно.

    base — содержимое файла на момент последней удачной синхронизации. Если оно
    есть, идём трёхсторонним путём; если нет, остаётся догадка по пустой стороне.
    """
    if base is not None:
        return build_blocks3(base, a, b)
    raw = []
    sm = SequenceMatcher(None, a, b, autojunk=False)
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        raw.append({"tag": "same" if tag == "equal" else "diff",
                    "left": a[i1:i2], "right": b[j1:j2]})
    merged = []
    for blk in raw:
        if merged and merged[-1]["tag"] == "diff" and blk["tag"] == "diff":
            merged[-1]["left"] += blk["left"]
            merged[-1]["right"] += blk["right"]
        else:
            merged.append(blk)
    for blk in merged:
        if blk["tag"] == "diff":
            # Без предка однозначна только односторонняя вставка: одна сторона пуста.
            blk["auto"] = (["left"] if blk["right"] and not blk["left"] else
                           ["right"] if blk["left"] and not blk["right"] else None)
    return _finish(merged)


def _finish(blocks):
    for i, blk in enumerate(blocks):
        blk["id"] = i
        if blk["tag"] == "same":
            blk["picks"] = ["left"]
        else:
            # Где сторона известна однозначно — ставим её сразу. Где спор —
            # по умолчанию побеждает локальная: её правил сам пользователь.
            blk["picks"] = list(blk.get("auto") or ["right"])
    return blocks


def block_result(blk):
    if blk["tag"] == "same":
        return list(blk["left"])
    out = []
    for side in blk.get("picks", []):
        out += blk["left"] if side == "left" else blk["right"]
    return out


def render_merged(blocks):
    out = []
    for b in blocks:
        out += block_result(b)
    return out


def rows_for_ui(blocks):
    """Строки сразу для трёх колонок. Каждый блок занимает одинаковое число строк
    во всех колонках, иначе панели разъезжаются, как только результат длиннее
    любой из сторон."""
    rows = []
    ln_l = ln_c = ln_r = 0
    for blk in blocks:
        if blk["tag"] == "same":
            for line in blk["left"]:
                ln_l += 1
                ln_c += 1
                ln_r += 1
                e = html.escape(line)
                rows.append({"kind": "same", "block": blk["id"],
                             "nl": ln_l, "nc": ln_c, "nr": ln_r, "l": e, "c": e, "r": e})
            continue
        res = block_result(blk)
        pairs = pair_lines(blk["left"], blk["right"])
        height = max(len(pairs), len(res), 1)
        for i in range(height):
            l, r = pairs[i] if i < len(pairs) else (None, None)
            row = {"kind": "diff", "block": blk["id"],
                   "nl": None, "nc": None, "nr": None, "l": "", "c": "", "r": ""}
            if l is not None:
                ln_l += 1
                row["nl"] = ln_l
            if r is not None:
                ln_r += 1
                row["nr"] = ln_r
            if l is not None and r is not None:
                row["l"], row["r"] = inline_html(l, r)
            else:
                row["l"] = html.escape(l) if l is not None else ""
                row["r"] = html.escape(r) if r is not None else ""
            if i < len(res):
                ln_c += 1
                row["nc"] = ln_c
                row["c"] = html.escape(res[i])
            rows.append(row)
    return rows


# ------------------------------------------------------------------ страница
PAGE = r"""<!doctype html>
<meta charset="utf-8">
<title>__NAME__</title>
<style>
:root{
  color-scheme:dark;
  --bg:#1e1f22; --panel:#2b2d30; --line:#393b40; --text:#dfe1e5; --dim:#8f9196;
  --srv:#4a2d2b; --srv-w:#7a3c38; --srv-ln:#3a2523;
  --loc:#25402e; --loc-w:#3a6b48; --loc-ln:#1f3325;
  --accent:#3574f0; --lnbg:#26282c; --mid:#202226;
}
*{box-sizing:border-box}
html,body{height:100%}
body{margin:0;background:var(--bg);color:var(--text);overflow:hidden;
     display:flex;flex-direction:column;
     font:14px/22px ui-monospace,SFMono-Regular,"SF Mono",Menlo,Consolas,monospace}
.ui{font-family:-apple-system,BlinkMacSystemFont,"SF Pro Text",Inter,sans-serif}

header{flex:none;background:var(--panel);border-bottom:1px solid var(--line);
       padding:0 16px;height:54px;display:flex;align-items:center;gap:10px;white-space:nowrap}
header .name{font:600 15px/1.3 -apple-system,BlinkMacSystemFont,sans-serif;color:#fff}
header .meta{font:12px/1.3 -apple-system,BlinkMacSystemFont,sans-serif;color:var(--dim);
             overflow:hidden;text-overflow:ellipsis}
.sp{flex:1}
.sep{width:1px;height:22px;background:var(--line)}
.btn{background:#393b40;color:#dfe1e5;border:1px solid #4a4c50;border-radius:7px;
     padding:6px 12px;font:13px/1.2 -apple-system,BlinkMacSystemFont,sans-serif;cursor:pointer}
.btn:hover{background:#43454a;border-color:#5a5c60}
.btn:active{transform:translateY(1px)}
.btn.icon{padding:6px 9px;font-size:13px}
.btn.go{background:var(--accent);border-color:var(--accent);color:#fff;font-weight:600}
.btn.go:hover{filter:brightness(1.15)}
.btn[disabled]{opacity:.35;cursor:default}
.count{font:13px/1.2 -apple-system,BlinkMacSystemFont,sans-serif;color:var(--dim);
       min-width:60px;text-align:center}

#main{flex:1;min-height:0;display:grid;grid-template-columns:1fr 16px}
#scroll{overflow-y:auto;overflow-x:hidden;position:relative}
.colhead{position:sticky;top:0;z-index:9;display:grid;grid-template-columns:var(--grid);
         background:var(--panel);border-bottom:1px solid var(--line);
         font:12px/30px -apple-system,BlinkMacSystemFont,sans-serif;color:var(--dim)}
.colhead div{padding:0 10px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.colhead .t{color:#c8cad0;font-weight:600}
#grid{display:grid;grid-template-columns:var(--grid);align-content:start;position:relative}
#rib{position:absolute;left:0;top:0;pointer-events:none;z-index:1}

.ln{padding:0 8px;text-align:right;color:#5d6066;background:var(--lnbg);
    user-select:none;position:relative;z-index:2}
.ln.l.d{background:var(--srv-ln);color:#b08a85}
.ln.r.d{background:var(--loc-ln);color:#86b095}
.ln.c{background:#232529}
.code{padding:0 10px;white-space:pre-wrap;word-break:break-word;min-height:22px;
      position:relative;z-index:2}
.code.l.d{background:var(--srv)}
.code.r.d{background:var(--loc)}
.code.c{background:var(--mid)}
.code.c.d{background:#2b2e34}
.code.c.fromL{background-color:#3b2b2e;
  background-image:linear-gradient(90deg,var(--srv-w) 0 3px,transparent 3px)}
.code.c.fromR{background-color:#26382c;
  background-image:linear-gradient(90deg,var(--loc-w) 0 3px,transparent 3px)}
.code.l.d em.w{background:var(--srv-w);font-style:normal;border-radius:3px;padding:1px 2px}
.code.r.d em.w{background:var(--loc-w);font-style:normal;border-radius:3px;padding:1px 2px}
.code.empty{background:repeating-linear-gradient(135deg,#26272b,#26272b 7px,#1e1f22 7px,#1e1f22 14px)}
.code.c.empty{background:repeating-linear-gradient(135deg,#232529,#232529 7px,#202226 7px,#202226 14px)}
.code.cur{box-shadow:inset 4px 0 0 var(--accent)}
.ln.cur{color:var(--accent);font-weight:600}
.code.c.d.bt{border-top-left-radius:6px;border-top-right-radius:6px}
.code.c.d.bb{border-bottom-left-radius:6px;border-bottom-right-radius:6px}

.gut{background:none;position:relative;z-index:2}
.gut .acts{position:absolute;top:1px;left:0;right:0;display:flex;justify-content:center;gap:2px}
.gut button{width:20px;height:18px;padding:0;border:0;background:none;cursor:pointer;
            display:flex;align-items:center;justify-content:center;border-radius:4px;
            color:#9aa0a6;transition:background .12s,color .12s}
.gut button svg{width:14px;height:14px;fill:none;stroke:currentColor;
                stroke-width:2;stroke-linecap:round;stroke-linejoin:round}
.gut button.dim{opacity:.4}
.gut:hover button.dim{opacity:.85}
.gut button:hover{background:rgba(255,255,255,.12);color:#fff}
.gut button.on{background:var(--accent);color:#fff;opacity:1}
.gut button.move{background:#3c3f45;color:#c8cad0;opacity:.85}
.gut button.move:hover{background:#4c5058;color:#fff}
.gut .ord{position:absolute;top:21px;left:0;right:0;text-align:center;
          font:10px/12px -apple-system,sans-serif;color:#7f8286}

#ruler{position:relative;background:#17181a;border-left:1px solid var(--line);cursor:pointer}
#ruler i{position:absolute;left:3px;right:3px;height:4px;border-radius:2px;background:#7a3c38}
#ruler i.ok{background:#3a6b48}
#ruler i.sel{background:var(--accent);height:6px}

footer{flex:none;background:var(--panel);border-top:1px solid var(--line);
       padding:9px 16px;display:flex;align-items:center;gap:10px;white-space:nowrap;
       font:13px/1.4 -apple-system,BlinkMacSystemFont,sans-serif;color:var(--dim)}
footer #stat{overflow:hidden;text-overflow:ellipsis;min-width:0}
kbd{background:#33353a;border:1px solid #4a4c50;border-bottom-width:2px;border-radius:4px;
    padding:1px 6px;font:11px/18px ui-monospace,monospace;color:#cfd2d6}
@media (max-width:1300px){.hints{display:none}}

#edit{position:absolute;inset:0;display:none;z-index:20;background:#202124}
#edit textarea{width:100%;height:100%;border:0;outline:none;resize:none;padding:12px 16px;
               background:#202124;color:var(--text);
               font:14px/22px ui-monospace,SFMono-Regular,Menlo,monospace}
</style>

<header>
  <span class="name">__NAME__</span>
  <span class="meta">__SUB__</span>
  <span class="sp"></span>
  <button class="btn" id="automerge" onclick="autoMerge()">Слить неконфликтующие</button>
  <span class="sep"></span>
  <button class="btn icon" id="undo" onclick="undo()">&#8630;</button>
  <button class="btn icon" id="redo" onclick="redo()">&#8631;</button>
  <span class="sep"></span>
  <button class="btn icon" onclick="jump(-1)" title="Предыдущее различие (Shift+F7)">&#9650;</button>
  <span class="count" id="pos">—</span>
  <button class="btn icon" onclick="jump(1)" title="Следующее различие (F7)">&#9660;</button>
  <span class="sep"></span>
  <button class="btn" id="editbtn" onclick="toggleEdit()">Править вручную</button>
</header>

<div id="main">
  <div id="scroll">
    <div class="colhead" id="colhead"></div>
    <div id="grid"><svg id="rib"></svg></div>
    <div id="edit"><textarea id="rtext" spellcheck="false"></textarea></div>
  </div>
  <div id="ruler"></div>
</div>

<footer>
  <span id="stat"></span>
  <span class="sp"></span>
  <span class="hints"><kbd>F7</kbd> различия &nbsp; <kbd>&#8592;</kbd><kbd>&#8594;</kbd> взять или вернуть
        &nbsp; <kbd>&#8984;Z</kbd> отмена</span>
  <button class="btn" onclick="send('cancel')">Отмена</button>
  <button class="btn go" onclick="send('apply')">Сохранить и синхронизировать</button>
</footer>

<script>
const D = __DATA__, MERGE = __MERGEABLE__;
document.documentElement.style.setProperty('--grid', '46px 1fr 54px 46px 1.05fr 54px 46px 1fr');
const diffIds = [...new Set(D.rows.filter(r => r.kind === 'diff').map(r => r.block))];
let cur = 0, manual = false, editing = false;
let hist = [], hpos = 0;

// Шеврон всегда показывает, куда поедет кусок. Не выбран — указывает в центр
// («взять»); выбран — наружу, к своей стороне («вернуть назад»).
const ICON = {
  toRight: 'M8 4 L16 12 L8 20',
  toLeft:  'M16 4 L8 12 L16 20',
  up:      'M12 19 L12 6 M6 12 L12 6 L18 12',
  down:    'M12 5 L12 18 M6 12 L12 18 L18 12',
};

function picksOf(id){ return D.blocks[id].picks || []; }
function hasPick(id, side){ return picksOf(id).indexOf(side) >= 0; }
function esc(s){ return s.replace(/[&<>]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;'}[c])); }

function mergedLines(){
  const out = [];
  for (const b of D.blocks) {
    if (b.tag === 'same') { out.push(...b.left); continue; }
    for (const s of (b.picks || [])) out.push(...(s === 'left' ? b.left : b.right));
  }
  return out;
}
// Нажатие на сторону включает или выключает её. Порядок нажатий = порядок строк
// в результате, поэтому отдельных кнопок «сначала левое» не нужно.
function toggle(id, side){
  const was = picksOf(id).slice(), now = was.slice();
  const i = now.indexOf(side);
  if (i >= 0) now.splice(i, 1); else now.push(side);
  step([{block: id, from: was, to: now}]);
}
function addSide(id, side, where){
  const was = picksOf(id).slice();
  if (was.indexOf(side) >= 0) return;
  const now = was.slice();
  if (where === 'start') now.unshift(side); else now.push(side);
  step([{block: id, from: was, to: now}]);
}
function removeSide(id, side){
  const was = picksOf(id).slice();
  step([{block: id, from: was, to: was.filter(s => s !== side)}]);
}
function movePick(id, side, dir){
  const was = picksOf(id).slice(), now = was.slice();
  const i = now.indexOf(side), j = i + dir;
  if (i < 0 || j < 0 || j >= now.length) return;
  now[i] = was[j]; now[j] = was[i];
  step([{block: id, from: was, to: now}]);
}
function step(changes){
  changes = changes.filter(c => JSON.stringify(c.from) !== JSON.stringify(c.to));
  if (!changes.length) return;
  hist = hist.slice(0, hpos);
  hist.push(changes);
  hpos = hist.length;
  changes.forEach(c => D.blocks[c.block].picks = c.to.slice());
  render();
}
function undo(){
  if (!hpos) return;
  hpos--;
  hist[hpos].forEach(c => D.blocks[c.block].picks = c.from.slice());
  const b = hist[hpos][0].block;
  if (diffIds.includes(b)) cur = diffIds.indexOf(b);
  render(); focusCur();
}
function redo(){
  if (hpos >= hist.length) return;
  hist[hpos].forEach(c => D.blocks[c.block].picks = c.to.slice());
  const b = hist[hpos][0].block;
  hpos++;
  if (diffIds.includes(b)) cur = diffIds.indexOf(b);
  render(); focusCur();
}
// auto считается на стороне Python. С общим предком это точный ответ: блок
// изменила ровно одна сторона. Без предка — только догадка по пустой стороне.
// Блоки, которые правили обе стороны, автослияние не трогает никогда.
function autoMerge(){
  const ch = [];
  for (const id of diffIds) {
    const a = D.blocks[id].auto;
    if (!a) continue;
    ch.push({block:id, from: picksOf(id).slice(), to: a.slice()});
  }
  step(ch);
}
function focusCur(){
  const el = document.querySelector('.code.c.cur') || document.querySelector('.code.cur');
  if (el) el.scrollIntoView({block:'center', behavior:'smooth'});
}

function cell(cls, htmlStr){
  const d = document.createElement('div');
  d.className = cls;
  if (htmlStr !== undefined) d.innerHTML = htmlStr;
  return d;
}
function head(){
  const h = document.getElementById('colhead');
  h.innerHTML = '';
  // «новее» относится к файлу целиком: узнать, какая строка правилась позже,
  // из времени файла нельзя.
  const L = D.leftLabel  + (D.newer === 'left'  ? '  ·  сохранён позже' : '');
  const R = D.rightLabel + (D.newer === 'right' ? '  ·  сохранён позже' : '');
  [['',''],['t',L],['',''],['',''],['t','Результат'],['',''],['',''],['t',R]]
    .forEach(([c,t]) => { const d = document.createElement('div');
                          if (c) d.className = c; d.textContent = t; h.appendChild(d); });
}
// Строки пересобираем при каждом изменении: высота блока зависит от того,
// сколько строк попало в результат.
function rowsForUI(){
  const rows = [];
  let nl = 0, nc = 0, nr = 0;
  for (const blk of D.blocks) {
    if (blk.tag === 'same') {
      blk.left.forEach((line, k) => {
        nl++; nc++; nr++;
        const e = blk.htmlSame[k];
        rows.push({kind:'same', block:blk.id, nl:nl, nc:nc, nr:nr, l:e, c:e, r:e});
      });
      continue;
    }
    const res = [], from = [];
    for (const s of (blk.picks || [])) {
      for (const line of (s === 'left' ? blk.left : blk.right)) { res.push(line); from.push(s); }
    }
    const h = Math.max(blk.pairs.length, res.length, 1);
    for (let i = 0; i < h; i++) {
      const p = blk.pairs[i] || [null, null, '', ''];
      const row = {kind:'diff', block:blk.id, nl:null, nc:null, nr:null, l:'', c:'', r:'', cf:null};
      if (p[0] !== null) { nl++; row.nl = nl; row.l = p[2]; }
      if (p[1] !== null) { nr++; row.nr = nr; row.r = p[3]; }
      if (i < res.length) { nc++; row.nc = nc; row.c = esc(res[i]); row.cf = from[i]; }
      rows.push(row);
    }
  }
  return rows;
}
function render(){
  D.rows = rowsForUI();
  const g = document.getElementById('grid');
  [...g.children].forEach(c => { if (c.id !== 'rib') c.remove(); });
  const first = {}, last = {}, seen = new Set();
  D.rows.forEach((r, i) => {
    if (r.kind !== 'diff') return;
    if (first[r.block] === undefined) first[r.block] = i;
    last[r.block] = i;
  });
  D.rows.forEach((r, i) => {
    const isDiff = r.kind === 'diff';
    const edge = isDiff ? ((i === first[r.block] ? ' bt' : '') + (i === last[r.block] ? ' bb' : '')) : '';
    const m = (isDiff ? ' d' : '') + (isDiff && r.block === diffIds[cur] ? ' cur' : '') + edge;
    const nl = cell('ln l' + m, r.nl === null ? '' : String(r.nl));
    const cl = cell('code l' + m + (r.nl === null ? ' empty' : ''), r.l);
    const gl = cell('gut gl' + (isDiff ? ' d' : ''));
    const nc = cell('ln c' + m, r.nc === null ? '' : String(r.nc));
    const cc = cell('code c' + m + (r.nc === null ? ' empty' : '')
                    + (r.cf === 'left' ? ' fromL' : r.cf === 'right' ? ' fromR' : ''), r.c);
    if (r.cf) cc.title = 'взято ' + (r.cf === 'left' ? 'с Яндекс.Диска' : 'с этого Mac');
    const gr = cell('gut gr' + (isDiff ? ' d' : ''));
    const nr = cell('ln r' + m, r.nr === null ? '' : String(r.nr));
    const cr = cell('code r' + m + (r.nr === null ? ' empty' : ''), r.r);
    if (isDiff) {
      cl.dataset.b = r.block; cc.dataset.b = r.block; cr.dataset.b = r.block;
      if (r.nl === null) cl.dataset.filler = '1';
      if (r.nc === null) cc.dataset.filler = '1';
      if (r.nr === null) cr.dataset.filler = '1';
      cl.style.opacity = hasPick(r.block, 'left')  ? '1' : '0.45';
      cr.style.opacity = hasPick(r.block, 'right') ? '1' : '0.45';
      [nl, cl, nc, cc, nr, cr].forEach(e => e.onclick = () => {
        cur = diffIds.indexOf(r.block); render();
      });
      if (MERGE && !seen.has(r.block)) {
        seen.add(r.block);
        const mk = (gut, side) => {
          // У пустой стороны кнопок нет: добавлять нечего, а они сбивают с толку.
          const blk = D.blocks[r.block];
          if (!(side === 'left' ? blk.left.length : blk.right.length)) return;
          const picks = picksOf(r.block);
          const k = picks.indexOf(side), on = k >= 0;
          const who = side === 'left' ? 'с Яндекс.Диска' : 'с этого Mac';
          const inward  = side === 'left' ? ICON.toRight : ICON.toLeft;
          const outward = side === 'left' ? ICON.toLeft  : ICON.toRight;
          const box = document.createElement('div'); box.className = 'acts';
          const btn = (icon, title, cls, fn) => {
            const b = document.createElement('button');
            b.className = cls; b.title = title;
            b.innerHTML = '<svg viewBox="0 0 24 24"><path d="' + icon + '"/></svg>';
            b.onclick = e => { e.stopPropagation(); fn(); };
            box.appendChild(b);
          };
          if (on) {
            // Стрелка развёрнута наружу: этот кусок уже в результате, нажатие его забирает.
            btn(outward, 'Вернуть назад, убрать из результата: ' + who, 'on',
                () => removeSide(r.block, side));
            if (picks.length > 1) {
              const last = k === picks.length - 1;
              btn(last ? ICON.up : ICON.down,
                  last ? 'Поднять этот кусок выше в результате'
                       : 'Опустить этот кусок ниже в результате', 'move',
                  () => movePick(r.block, side, last ? -1 : 1));
            }
          } else {
            btn(inward, 'Добавить в результат ' + (picks.length ? 'вниз: ' : ': ') + who, 'dim',
                () => addSide(r.block, side, 'end'));
            if (picks.length) {
              btn(ICON.up, 'Добавить в результат вверх: ' + who, 'dim',
                  () => addSide(r.block, side, 'start'));
            }
          }
          gut.appendChild(box);
          if (on && picks.length > 1) {
            const ord = document.createElement('div');
            ord.className = 'ord'; ord.textContent = String(k + 1);
            ord.title = 'Порядок кусков в результате';
            gut.appendChild(ord);
          }
        };
        mk(gl, 'left');
        mk(gr, 'right');
      }
    }
    [nl, cl, gl, nc, cc, gr, nr, cr].forEach(e => g.appendChild(e));
  });
  ribbons(); ruler(); status();
  if (!manual) document.getElementById('rtext').value = mergedLines().join('\n');
}
function ribbons(){
  const g = document.getElementById('grid');
  const svg = document.getElementById('rib');
  const gl = g.querySelector('.gut.gl'), gr = g.querySelector('.gut.gr');
  svg.innerHTML = '';
  if (!gl || !gr) return;
  const H = g.scrollHeight;
  svg.setAttribute('width', g.clientWidth); svg.setAttribute('height', H);
  const rect = (x, w, fill) => {
    const e = document.createElementNS('http://www.w3.org/2000/svg','rect');
    e.setAttribute('x', x); e.setAttribute('y', 0); e.setAttribute('width', w);
    e.setAttribute('height', H); e.setAttribute('fill', fill); svg.appendChild(e);
  };
  rect(gl.offsetLeft, gl.offsetWidth, '#181a1c');
  rect(gr.offsetLeft, gr.offsetWidth, '#181a1c');
  const band = (gut, from, to, fill, sel) => {
    const x0 = gut.offsetLeft, w = gut.offsetWidth, xm = x0 + w / 2;
    const p = document.createElementNS('http://www.w3.org/2000/svg','path');
    p.setAttribute('d', 'M ' + x0 + ',' + from[0] +
      ' C ' + xm + ',' + from[0] + ' ' + xm + ',' + to[0] + ' ' + (x0+w) + ',' + to[0] +
      ' L ' + (x0+w) + ',' + to[1] +
      ' C ' + xm + ',' + to[1] + ' ' + xm + ',' + from[1] + ' ' + x0 + ',' + from[1] + ' Z');
    p.setAttribute('fill', fill);
    p.setAttribute('stroke', sel ? '#5b9cff' : 'rgba(255,255,255,.16)');
    p.setAttribute('stroke-width', sel ? '1.6' : '1');
    svg.appendChild(p);
  };
  const ext = sel => {
    let els = [...g.querySelectorAll(sel + ':not([data-filler])')];
    if (!els.length) els = [...g.querySelectorAll(sel)];
    if (!els.length) return null;
    return [els[0].offsetTop, els[els.length-1].offsetTop + els[els.length-1].offsetHeight];
  };
  for (const id of diffIds) {
    const L = ext('.code.l[data-b="' + id + '"]');
    const C = ext('.code.c[data-b="' + id + '"]');
    const R = ext('.code.r[data-b="' + id + '"]');
    const sel = id === diffIds[cur];
    if (L && C) band(gl, L, C, hasPick(id,'left')  ? 'rgba(140,66,62,.72)' : 'rgba(120,124,130,.18)', sel);
    if (C && R) band(gr, C, R, hasPick(id,'right') ? 'rgba(64,122,82,.72)' : 'rgba(120,124,130,.18)', sel);
  }
}
function ruler(){
  const el = document.getElementById('ruler');
  el.innerHTML = '';
  const total = D.rows.length || 1;
  D.rows.forEach((r, i) => {
    if (r.kind !== 'diff') return;
    const m = document.createElement('i');
    m.style.top = (i / total * 100) + '%';
    m.className = (r.block === diffIds[cur]) ? 'sel' : (D.blocks[r.block].auto ? 'ok' : '');
    el.appendChild(m);
  });
  el.onclick = e => {
    const sc = document.getElementById('scroll');
    sc.scrollTo({top: e.offsetY / el.clientHeight * sc.scrollHeight, behavior:'smooth'});
  };
}
function status(){
  const u = document.getElementById('undo'), r = document.getElementById('redo');
  u.disabled = !hpos; r.disabled = hpos >= hist.length;
  u.title = hpos ? ('Отменить шаг (' + hpos + ') — ⌘Z') : 'Отменять нечего';
  r.title = (hpos < hist.length) ? 'Вернуть шаг — ⇧⌘Z' : 'Возвращать нечего';
  const one = diffIds.filter(i => D.blocks[i].auto).length;
  const pending = diffIds.filter(i => D.blocks[i].auto &&
      JSON.stringify(D.blocks[i].auto) !== JSON.stringify(picksOf(i))).length;
  const am = document.getElementById('automerge');
  am.disabled = !MERGE || !pending;
  am.title = !one ? 'Однозначных изменений нет — всё требует решения'
    : (D.threeWay
        ? ('Изменено ровно одной стороной: ' + one + '. Определено по общему предку — ' +
           'снимку последней удачной синхронизации. Блоки, которые правили обе стороны, ' +
           'не трогаются.')
        : ('Односторонних вставок: ' + one + '. Общего предка для этого файла нет, ' +
           'поэтому однозначны только вставки: отличить «добавили» от «удалили» нечем.'))
       + (pending ? '' : ' Уже применено.');
  document.getElementById('pos').textContent =
    diffIds.length ? ((cur + 1) + ' из ' + diffIds.length) : 'нет различий';
  const real = diffIds.length - one;
  document.getElementById('stat').textContent = !MERGE
    ? ('Различий: ' + diffIds.length + ' · файл офисный: выберите версию целиком')
    : ('Различий: ' + diffIds.length + ' · однозначных ' + one + ' · спорных ' + real +
       (D.threeWay ? ' (по общему предку)' : ' (предка нет, счёт приблизительный)') +
       ' · стрелка внутрь берёт кусок, наружу возвращает; ↑↓ меняют порядок в результате');
}
function jump(d){
  if (!diffIds.length) return;
  cur = (cur + d + diffIds.length) % diffIds.length;
  render(); focusCur();
}
function toggleEdit(){
  editing = !editing;
  document.getElementById('edit').style.display = editing ? 'block' : 'none';
  document.getElementById('editbtn').textContent =
    editing ? 'Вернуться к слиянию' : (manual ? 'Править вручную · изменён' : 'Править вручную');
  if (editing && !manual) document.getElementById('rtext').value = mergedLines().join('\n');
}
function send(action){
  const body = {action, picks: D.blocks.map(b => b.picks || [])};
  if (MERGE) body.text = document.getElementById('rtext').value;
  fetch('/apply', {method:'POST', headers:{'Content-Type':'application/json'},
                   body: JSON.stringify(body)})
   .then(r => r.text()).then(t => {
     document.body.innerHTML =
       '<div class="ui" style="padding:64px;font-size:15px;line-height:1.7;color:#dfe1e5">'
       + t.replace(/</g,'&lt;') + '<br><br><span style="color:#8f9196">Окно можно закрыть.</span></div>';
   });
}
document.getElementById('rtext').addEventListener('input', () => {
  manual = true;
  document.getElementById('editbtn').textContent = 'Вернуться к слиянию · изменён вручную';
});
document.addEventListener('keydown', e => {
  if (e.target && e.target.id === 'rtext') return;
  if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === 'z') {
    e.shiftKey ? redo() : undo(); e.preventDefault(); return;
  }
  if (e.key === 'F7') { jump(e.shiftKey ? -1 : 1); e.preventDefault(); return; }
  if (!MERGE || !diffIds.length) return;
  const id = diffIds[cur];
  if (e.key === 'ArrowLeft')  { toggle(id, 'left');  e.preventDefault(); }
  if (e.key === 'ArrowRight') { toggle(id, 'right'); e.preventDefault(); }
});
window.addEventListener('resize', ribbons);
head(); render();
</script>
"""


def stamp(path):
    """Размер и время правки — то, что про файл вообще известно снаружи."""
    import time
    st = os.stat(path)
    return "%s  ·  %d байт" % (time.strftime("%d.%m %H:%M", time.localtime(st.st_mtime)),
                               st.st_size)


def page(conflict, blocks, mergeable, reader, three_way=False):
    name = html.escape(os.path.basename(conflict["base"]))
    sub = "%s  ·  читаем как: %s  ·  %s" % (
        html.escape(conflict["folder"]), reader,
        "есть общий предок" if three_way else "общего предка нет")
    if not mergeable:
        sub += "  ·  поблочное слияние недоступно"
    # Заготовки строк считаем здесь: в браузере они не меняются, меняется только выбор.
    for blk in blocks:
        if blk["tag"] == "same":
            blk["htmlSame"] = [html.escape(x) for x in blk["left"]]
        else:
            pairs = []
            for l, r in pair_lines(blk["left"], blk["right"]):
                if l is not None and r is not None:
                    hl, hr = inline_html(l, r)
                else:
                    hl = html.escape(l) if l is not None else ""
                    hr = html.escape(r) if r is not None else ""
                pairs.append([l, r, hl, hr])
            blk["pairs"] = pairs
    data = {
        "leftLabel": "Яндекс.Диск  ·  %s" % stamp(conflict["remote"]),
        "rightLabel": "Этот Mac  ·  %s" % stamp(conflict["local"]),
        "newer": "left" if os.path.getmtime(conflict["remote"])
                 > os.path.getmtime(conflict["local"]) else "right",
        "threeWay": bool(three_way),
        "blocks": blocks,
        "rows": rows_for_ui(blocks),
    }
    return (PAGE.replace("__NAME__", name)
                .replace("__SUB__", sub)
                .replace("__DATA__", json.dumps(data, ensure_ascii=False))
                .replace("__MERGEABLE__", "true" if mergeable else "false"))
