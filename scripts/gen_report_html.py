# -*- coding: utf-8 -*-
"""
把「分析报告.md」转成符合 doc-typeset 规范的专业 HTML（business-report 风格）
================================================================================
为什么要有这一步：docx 转换器（html4docx）对 CSS 的还原是有限的
——不支持 CSS Grid、不支持 <dl>/<dt>/<dd>、装饰性短横线会退化成满版横线。
所以必须在 HTML 侧就按"可安全转换"的写法产出：
  · 结构化内容一律用 <table>
  · 每个块级元素显式声明关键属性（不做继承依赖）
  · 样式全部走 CSS 变量（禁止裸值）
  · 用 <section role> + @page 表达封面/正文与页码

用法：python -u scripts/gen_report_html.py
输出：dataset_boards/六榜数据集/分析报告.html
"""
import base64
import json
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATASET_DIR = os.path.join(ROOT, 'dataset_boards', '六榜数据集')
SRC_MD = os.path.join(DATASET_DIR, '分析报告.md')
OUT_HTML = os.path.join(DATASET_DIR, '分析报告.html')

# 图片统一宽度（px）。转换器内部会钳到 396pt（13.97cm），正好适配 A4 版心。
IMG_WIDTH = 620
# 这个字符串必须与 html4docx 的 get_image_alignment() 完全一致才会被识别成居中；
# 多一个空格、或换成 var() 都会退化成左对齐 —— 故刻意写裸值，不走 CSS 变量。
IMG_CENTER_STYLE = 'display: block; margin-left: auto; margin-right: auto;'
# 设计令牌文件（tencent-docx 插件产物）。定位不到就用下面内置的兜底变量表，
# 效果等价 —— 因此这里不写死本机路径，改为环境变量传入。
_PLUGIN_DIR = os.environ.get('TENCENT_DOCX_PLUGIN', '')
TOKENS_JSON = (os.path.join(_PLUGIN_DIR, 'skills', 'design-token', 'tokens',
                            'compiled', 'business-report.json')
               if _PLUGIN_DIR else '')


def load_css_vars():
    """从已编译的设计令牌里取 CSS 变量（design-token skill 的静态查表产物）。"""
    fallback = {
        '--color-primary': '#1565C0', '--color-secondary': '#0288D1',
        '--color-accent': '#00897B', '--color-text': '#212121',
        '--color-textSecondary': '#616161', '--color-heading': '#1A237E',
        '--color-divider': '#E0E0E0', '--color-surface': '#F5F7FA',
        '--color-dataViz-series1': '#1565C0', '--color-dataViz-series2': '#00897B',
        '--color-dataViz-series3': '#F9A825', '--color-dataViz-series4': '#E53935',
        '--color-dataViz-series5': '#7B1FA2', '--color-dataViz-series6': '#0288D1',
    }
    try:
        d = json.load(open(TOKENS_JSON, encoding='utf-8'))
        return d.get('css_variables') or fallback
    except Exception:                                           # noqa: BLE001
        return fallback


def inline(t):
    """行内标记：**粗体** / `code`。"""
    t = re.sub(r'\*\*(.+?)\*\*', r'<strong>\1</strong>', t)
    t = re.sub(r'`(.+?)`', r'<code>\1</code>', t)
    return t.strip()


def md_table_to_html(lines):
    """把 Markdown 表格转成 <table>（docx 唯一可靠的结构承载方式）。"""
    rows = []
    for ln in lines:
        cells = [c.strip() for c in ln.strip().strip('|').split('|')]
        rows.append(cells)
    if len(rows) >= 2 and set(rows[1][0]) <= set('-: '):
        header, body = rows[0], rows[2:]
    else:
        header, body = None, rows
    out = ['<table>']
    if header:
        out.append('<tr>' + ''.join(
            f'<td><strong>{inline(c)}</strong></td>' for c in header) + '</tr>')
    for r in body:
        out.append('<tr>' + ''.join(f'<td>{inline(c)}</td>' for c in r) + '</tr>')
    out.append('</table>')
    return '\n'.join(out)


def _data_uri(fname):
    """把图读成 base64 data URI。找不到返回 None（调用方降级成占位提示）。"""
    p = os.path.join(DATASET_DIR, fname)
    if not os.path.isfile(p):
        return None
    ext = (os.path.splitext(fname)[1].lstrip('.').lower() or 'png')
    mime = 'jpeg' if ext in ('jpg', 'jpeg') else ext
    with open(p, 'rb') as f:
        b64 = base64.b64encode(f.read()).decode('ascii')
    return f'data:image/{mime};base64,{b64}'


def figure_html(fname, caption):
    """生成「居中图片 + 居中图注」两个块。

    docx 转换器的图片处理有两条硬约束（读源码确认）：
      ① width 只按 px 解析，且内部钳到 MAX_INDENT*72 = 396pt，写大也只会到 13.97cm；
      ② 对齐判定是**字符串全等**比较，样式必须逐字一致，否则退回左对齐。
    """
    uri = _data_uri(fname)
    if not uri:
        return (f'<p style="text-align:center; color:#C62828;">'
                f'[ 缺图：{fname} ]</p>')
    img = (f'<img src="{uri}" width="{IMG_WIDTH}" '
           f'style="{IMG_CENTER_STYLE}">')
    cap = (f'<p style="text-align:center; font-size:8.5pt; color:#616161; '
           f'margin-top:3pt;">{inline(caption)}</p>')
    return f'<p style="text-align:center;">{img}</p>\n{cap}'


def md_to_blocks(md):
    """把 Markdown 转成 HTML 块序列（h1/h2/h3/p/table/ul/quote/figure）。"""
    blocks, i = [], 0
    lines = md.split('\n')
    n = len(lines)
    while i < n:
        ln = lines[i]
        s = ln.strip()
        if not s:
            i += 1
            continue
        # 图片：![图注](文件名.png) —— 单独成行，转成居中图 + 图注
        m_img = re.match(r'^!\[(.*?)\]\((.+?)\)\s*$', s)
        if m_img:
            blocks.append(figure_html(m_img.group(2).strip(),
                                      m_img.group(1).strip()))
            i += 1
            continue
        # 表格
        if s.startswith('|'):
            j = i
            buf = []
            while j < n and lines[j].strip().startswith('|'):
                buf.append(lines[j])
                j += 1
            blocks.append(md_table_to_html(buf))
            i = j
            continue
        # 标题
        m = re.match(r'^(#{1,4})\s+(.*)$', s)
        if m:
            lv = min(len(m.group(1)), 3)
            blocks.append(f'<h{lv}>{inline(m.group(2))}</h{lv}>')
            i += 1
            continue
        # 引用
        if s.startswith('>'):
            buf = []
            while i < n and lines[i].strip().startswith('>'):
                buf.append(lines[i].strip().lstrip('>').strip())
                i += 1
            txt = ' '.join(x for x in buf if x)
            blocks.append(
                '<p style="border-left:4px solid var(--color-primary);'
                'background:var(--color-surface);padding:6pt 10pt;">'
                f'&nbsp;&nbsp;{inline(txt)}</p>')
            continue
        # 有序 / 无序列表
        if re.match(r'^(\d+\.|[-*])\s+', s):
            ordered = bool(re.match(r'^\d+\.', s))
            items = []
            while i < n and re.match(r'^(\d+\.|[-*])\s+', lines[i].strip()):
                items.append(re.sub(r'^(\d+\.|[-*])\s+', '',
                                    lines[i].strip()))
                i += 1
            tag = 'ol' if ordered else 'ul'
            lis = ''.join(f'<li>{inline(x)}</li>' for x in items)
            blocks.append(f'<{tag}>{lis}</{tag}>')
            continue
        # 普通段落（合并连续行）
        buf = []
        while i < n and lines[i].strip() and not re.match(
                r'^(#{1,4}\s|\||>|\d+\.\s|[-*]\s|!\[)', lines[i].strip()):
            buf.append(lines[i].strip())
            i += 1
        if buf:
            blocks.append(f'<p>{inline(" ".join(buf))}</p>')
        else:
            i += 1
    return blocks


def build_html():
    md = open(SRC_MD, encoding='utf-8').read()
    cv = load_css_vars()
    vars_css = '\n'.join(f'    {k}: {v};' for k, v in cv.items())

    # 正文：去掉一级标题（标题进封面），其余转 HTML
    body_md = re.sub(r'^#\s+.*$', '', md, count=1, flags=re.M)
    html_body = '\n'.join(md_to_blocks(body_md))

    style = f"""
  :root {{
{vars_css}
    --page-content-width: 16cm;
  }}
  body {{
    font-family: var(--typography-fontFamily-body, "微软雅黑");
    font-size: var(--typography-fontSize-body, 9pt);
    line-height: var(--typography-lineHeight-body, 1.6);
    color: var(--color-text);
    max-width: var(--page-content-width);
    margin-left: auto;
    margin-right: auto;
  }}
  /* 封面：每个块自己声明对齐，不依赖继承（docx 不做 CSS 继承计算） */
  .cover-eyebrow {{ text-align: center; font-size: var(--typography-fontSize-coverCategory,14pt);
                   color: var(--color-primary); }}
  h1.cover-title {{ text-align: center; font-size: var(--typography-fontSize-coverTitle,22pt);
                    color: var(--color-heading); line-height: 1.35; }}
  p.cover-sub {{ text-align: center; font-size: 11pt; color: var(--color-textSecondary); }}
  .cover-meta td {{ text-align: center; font-size: 10pt; color: var(--color-textSecondary);
                    padding: 3pt 0; border: none; }}
  .cover-meta {{ margin-left: auto; margin-right: auto; width: 11cm; }}

  h1 {{ font-size: var(--typography-fontSize-h1,20pt); color: var(--color-heading);
        font-weight: 600; border-left: 5px solid var(--color-primary);
        padding-left: 8pt; margin-top: 20pt; margin-bottom: 8pt; }}
  h2 {{ font-size: var(--typography-fontSize-h2,11pt); color: var(--color-primary);
        font-weight: 600; margin-top: 12pt; margin-bottom: 6pt; }}
  h3 {{ font-size: var(--typography-fontSize-h3,10pt); color: var(--color-heading);
        font-weight: 600; margin-top: 10pt; margin-bottom: 5pt; }}
  p  {{ font-size: var(--typography-fontSize-body,9pt); color: var(--color-text);
        line-height: var(--typography-lineHeight-body,1.6); margin: 5pt 0; }}
  li {{ font-size: var(--typography-fontSize-body,9pt); color: var(--color-text);
        line-height: 1.55; margin: 3pt 0; }}
  table {{ border-collapse: collapse; width: 100%; margin: 8pt 0; }}
  td {{ border: 1px solid var(--color-divider); padding: 4pt 6pt;
        font-size: var(--typography-fontSize-small,8pt); color: var(--color-text);
        vertical-align: middle; }}
  table tr:first-child td {{ background: var(--color-surface); font-weight: 600; }}
  code {{ font-family: Consolas, monospace; font-size: 8.5pt;
          color: var(--color-primary); }}
  strong {{ font-weight: 600; color: var(--color-heading); }}

  /* 页码：正文页脚居中；封面不产页脚 */
  @page {{ @bottom-center {{ content: "第 " counter(page) " 页 / 共 " counter(pages) " 页"; }} }}
  @page cover {{ @bottom-center {{ content: none; }} }}
  section[role="cover"] {{ page: cover; }}
"""

    html = f"""<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<meta name="docx-page-size" content="A4">
<style>{style}</style>
</head>
<body>
<section role="cover">
  <p class="cover-eyebrow">&nbsp;</p>
  <h1 class="cover-title">微博六榜单热搜热度追踪<br>分析报告</h1>
  <p class="cover-sub">文娱 · 生活 · 社会 · 体育 · 科技 · ACG</p>
  <table class="cover-meta">
    <tr><td>采集时间：2026-09-22 22:41 — 2026-09-23 09:00</td></tr>
    <tr><td>采样频率：每 30 分钟一轮</td></tr>
    <tr><td>数据规模：583 个话题 · 6028 个热度样本 · 25445 条评论</td></tr>
  </table>
</section>
<section role="body">
{html_body}
</section>
</body>
</html>
"""
    os.makedirs(os.path.dirname(OUT_HTML), exist_ok=True)
    with open(OUT_HTML, 'w', encoding='utf-8') as f:
        f.write(html)
    print('✓', OUT_HTML, os.path.getsize(OUT_HTML), 'B')
    return 0


if __name__ == '__main__':
    sys.exit(build_html())
