# -*- coding: utf-8 -*-
"""
多榜单追踪结果导出
================================================================================
从 data/multi_boards.db 导出**突出热度变化**的交付套件：

  事件表.xlsx        每话题一行：六榜话题 + 关键热度指标（首次/峰值/最新/变化率）
  热度明细.xlsx      长表：话题 × 采样时刻 × 热度 × 榜内排名 —— 直接可画曲线
  事件评论信息.xlsx   评论（内容 + 点赞数 + 一级/二级标识）
  热度曲线_Top10.png  热度 Top10 话题的时间序列曲线
  榜单对比.png        各榜话题数 / 峰值热度 / 评论量对比
  热度与评论分布.png   峰值热度与评论数的分布关系

用法：python -u scripts/export_boards.py
      （需 matplotlib；若主环境未装，可用任意装有 matplotlib 的解释器运行）
"""
import os
import sqlite3
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone

import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB = os.path.join(ROOT, 'data', 'multi_boards.db')
OUT = os.path.join(ROOT, 'dataset_boards', '六榜数据集')
CST = timezone(timedelta(hours=8))

PLT_FONT = ['Microsoft YaHei', 'SimHei', 'SimSun']


# ------------------------------------------------------------------ 工具

def san(x):
    """去掉 Excel 不允许的控制字符。"""
    if x is None:
        return ''
    s = str(x)
    return ''.join(ch for ch in s if ch == '\n' or ord(ch) >= 32)


def _disp_len(s):
    n = 0
    for ch in str(s):
        n += 2 if ord(ch) > 0x2E80 else 1
    return n


def _wrapped_lines(text, col_width):
    if not text:
        return 1
    total, line = 0, 0
    for ch in str(text):
        w = 2 if ord(ch) > 0x2E80 else 1
        if line + w > col_width:
            total += 1
            line = w
        else:
            line += w
    return total + 1


def style_sheet(ws, widths, max_row_height=409):
    """统一列宽 + 行高按内容自适应 + 自动换行 + 冻结表头。"""
    head_fill = PatternFill('solid', fgColor='DDEBF7')
    for i, w in enumerate(widths, 1):
        ws.column_dimensions[get_column_letter(i)].width = w
        c = ws.cell(1, i)
        c.font = Font(bold=True)
        c.fill = head_fill
        c.alignment = Alignment(horizontal='center', vertical='center',
                                wrap_text=True)
    for r in range(1, ws.max_row + 1):
        need = 1
        for i, w in enumerate(widths, 1):
            v = ws.cell(r, i).value
            if v is None or v == '':
                continue
            need = max(need, _wrapped_lines(v, max(6, w - 1)))
        h = min(max_row_height, need * 14.5 + 4)
        ws.row_dimensions[r].height = 16 if r == 1 else h
        for i in range(1, len(widths) + 1):
            ws.cell(r, i).alignment = Alignment(vertical='center',
                                                wrap_text=True)
    ws.freeze_panes = 'A2'


def hm(dt_str):
    """只取 HH:MM。"""
    return dt_str[11:16] if dt_str and len(dt_str) >= 16 else ''


def mins_between(a, b):
    try:
        ta = datetime.strptime(a, '%Y-%m-%d %H:%M:%S')
        tb = datetime.strptime(b, '%Y-%m-%d %H:%M:%S')
        return int((tb - ta).total_seconds() // 60)
    except Exception:                                           # noqa: BLE001
        return None


def fig_path(name):
    os.makedirs(OUT, exist_ok=True)
    return os.path.join(OUT, name)


# ------------------------------------------------------------------ 导出

def main():
    os.makedirs(OUT, exist_ok=True)
    c = sqlite3.connect(DB)
    cur = c.cursor()

    # ---------- 取话题主数据 ----------
    topics = cur.execute("""
        SELECT t.topic_id, t.word, b.name AS board, t.category,
               t.first_seen_ts, t.last_seen_ts, t.onboard_count,
               t.peak_heat, t.latest_heat, t.is_active
        FROM topics t JOIN boards b ON b.board_id = t.board_id
        ORDER BY t.peak_heat DESC
    """).fetchall()

    posts = {r[0]: r for r in cur.execute("""
        SELECT topic_id, mid, text, screen_name, created_at,
               reposts_count, comments_count, attitudes_count, region_name, source
        FROM topic_posts
    """)}

    # 区段（上榜/下榜时间）：取第一段与最后一段
    segs = defaultdict(list)
    for r in cur.execute("""SELECT topic_id, seg_no, onboard_ts, offboard_ts,
                                   offboard_upper_ts
                            FROM topic_segments ORDER BY seg_no"""):
        segs[r[0]].append(r)

    # 热度序列
    heat = defaultdict(list)
    for r in cur.execute("""SELECT topic_id, seg_no, sample_ts, num, rank
                            FROM heat_samples ORDER BY sample_ts"""):
        heat[r[0]].append(r)

    # 评论数
    ncmt = dict(cur.execute("SELECT topic_id, COUNT(*) FROM comments "
                            "GROUP BY topic_id").fetchall())

    # ---------- 事件表 ----------
    hdr1 = ['序号', '事件名', '榜单', '类型', '事件内容', '来源',
            '事件发生时间', '上头条热榜时间', '下头条热榜时间',
            '在榜时长(分)', '首次热度', '峰值热度', '最新热度',
            '热度变化率', '采样点数', '评论数', '评论信息']
    w1 = [6, 26, 8, 12, 66, 16, 19, 19, 19, 12, 12, 12, 12, 11, 9, 8, 10]
    rows1 = []
    for i, (tid, word, board, cat, fs, ls, obc, peak, latest, act) in enumerate(topics, 1):
        p = posts.get(tid)
        sg = segs.get(tid) or []
        ob_ts = sg[0][2] if sg else fs
        if sg:
            last = sg[-1]
            off_ts = last[4] or last[3] or ''
        else:
            off_ts = ''
        hs = heat.get(tid) or []
        nums = [h[3] for h in hs if h[3] is not None]
        first_h = nums[0] if nums else None
        peak_h = max(nums) if nums else None
        last_h = nums[-1] if nums else None
        rate = ''
        if first_h and last_h and first_h > 0:
            rate = f'{(last_h - first_h) / first_h * 100:+.0f}%'
        dur = ''
        if ob_ts and off_ts:
            dur = mins_between(ob_ts, off_ts)
        rows1.append([
            i, san(word), san(board), san(cat),
            san(p[2]) if p else '', san(p[3]) if p else '',
            san(p[4]) if p else '', san(ob_ts), san(off_ts),
            dur if dur is not None else '',
            first_h if first_h is not None else '',
            peak_h if peak_h is not None else '',
            last_h if last_h is not None else '',
            rate, len(hs), ncmt.get(tid, 0), f'R{i}',
        ])

    wb = Workbook()
    ws = wb.active
    ws.title = '事件表'
    ws.append(hdr1)
    for r in rows1:
        ws.append(r)
    style_sheet(ws, w1)
    p1 = os.path.join(OUT, '事件表.xlsx')
    wb.save(p1)
    print(f'✓ {p1}  ({len(rows1)} 行)')

    # ---------- 热度明细（长表，直接可画曲线）----------
    hdr2 = ['事件名', '榜单', '采样时刻', '采样序号', '距首次采样(分钟)', '热度值', '榜内排名', '区间']
    w2 = [26, 8, 19, 10, 18, 12, 10, 16]
    rows2 = []
    t_idx = {tid: i for i, (tid, *_r) in enumerate(topics, 1)}
    for tid, word, board, *_rest in topics:
        hs = heat.get(tid) or []
        if not hs:
            continue
        base = hs[0][2]
        for k, (t2, seg_no, ts, num, rank) in enumerate(hs, 1):
            rows2.append([san(word), san(board), san(ts), k,
                          mins_between(base, ts), num if num is not None else '',
                          rank if rank is not None else '', f'第{seg_no}段'])
    wb2 = Workbook()
    ws2 = wb2.active
    ws2.title = '热度明细'
    ws2.append(hdr2)
    for r in rows2:
        ws2.append(r)
    style_sheet(ws2, w2)
    p2 = os.path.join(OUT, '热度明细.xlsx')
    wb2.save(p2)
    print(f'✓ {p2}  ({len(rows2)} 行)')

    # ---------- 评论表 ----------
    hdr3 = ['ID', '事件名', '评论者', '内容', '点赞数', '类型']
    w3 = [8, 24, 18, 70, 10, 10]
    rows3 = []
    for tid, word, *_r in topics:
        i = t_idx[tid]
        for sn, txt, lk, rep in cur.execute("""
                SELECT screen_name, text, like_count, is_reply FROM comments
                WHERE topic_id=? ORDER BY COALESCE(like_count,0) DESC""", (tid,)):
            rows3.append([f'R{i}', san(word), san(sn), san(txt),
                          lk if lk is not None else 0,
                          '二级回复' if rep else '一级评论'])
    wb3 = Workbook()
    ws3 = wb3.active
    ws3.title = '评论'
    ws3.append(hdr3)
    for r in rows3:
        ws3.append(r)
    style_sheet(ws3, w3)
    p3 = os.path.join(OUT, '事件评论信息.xlsx')
    wb3.save(p3)
    print(f'✓ {p3}  ({len(rows3)} 行)')

    # ---------- 图表 ----------
    plt.rcParams['font.sans-serif'] = PLT_FONT
    plt.rcParams['axes.unicode_minus'] = False
    os.makedirs(OUT, exist_ok=True)

    # 图1：热度 Top10 曲线（x 轴用**绝对时间**，因为各话题上榜时刻不同，
    # 用"序号"会让时间轴错位——踩过这个坑）
    top10 = [t for t in topics if (heat.get(t[0]) or [])][:10]
    if top10:
        import matplotlib.dates as mdates
        # 统一时间轴：话题下榜后没有样本，用 NaN 占位 → 折线自动断开，
        # 不会把"下榜"和"后来复上榜"的点硬连起来造成跳变错觉。
        all_ts = sorted({h[2] for hs in heat.values() for h in hs})
        tmap = {ts: i for i, ts in enumerate(all_ts)}
        axis_t = [datetime.strptime(ts, '%Y-%m-%d %H:%M:%S') for ts in all_ts]
        fig, ax = plt.subplots(figsize=(13.5, 6.8))
        for tid, word, board, *_r in top10:
            ys = [float('nan')] * len(all_ts)
            for h in heat[tid]:
                ys[tmap[h[2]]] = (h[3] or 0)
            ax.plot(axis_t, ys, marker='o', ms=3.8, lw=1.6,
                    label=f'{word[:16]}（{board}）')
        ax.xaxis.set_major_formatter(mdates.DateFormatter('%m-%d %H:%M'))
        ax.xaxis.set_major_locator(mdates.HourLocator(interval=1))
        plt.setp(ax.get_xticklabels(), rotation=55, ha='right', fontsize=8)
        ax.set_xlabel('采样时刻（折线断开 = 该话题当时已下榜）')
        ax.set_ylabel('热度值')
        ax.set_title('热度 Top10 话题 · 热度变化曲线（30 分钟一采样）')
        ax.legend(fontsize=8.5, loc='upper right')
        ax.grid(alpha=0.3)
        plt.tight_layout()
        plt.savefig(fig_path('热度曲线_Top10.png'), dpi=130)
        plt.close()
        print('✓ 热度曲线_Top10.png')

    # 图2：各榜对比
    board_stat = defaultdict(lambda: [0, 0, 0])   # 话题数, 热度样本, 评论数
    for tid, word, board, *_r in topics:
        board_stat[board][0] += 1
        board_stat[board][1] += len(heat.get(tid) or [])
        board_stat[board][2] += ncmt.get(tid, 0)
    bs = sorted(board_stat.items(), key=lambda kv: -kv[1][0])
    if bs:
        fig, axes = plt.subplots(1, 3, figsize=(15, 4.6))
        names = [k for k, _ in bs]
        for ax, idx, title in zip(axes, (0, 1, 2),
                                  ('话题数', '热度采样点数', '评论数')):
            vals = [v[idx] for _, v in bs]
            bars = ax.bar(names, vals, color=['#c0504d', '#4f81bd', '#9bbb59',
                                              '#f79646', '#8064a2', '#4bacc6'][:len(names)])
            ax.set_title(f'各榜 · {title}')
            for b, v in zip(bars, vals):
                ax.text(b.get_x() + b.get_width() / 2, v, str(v),
                        ha='center', va='bottom', fontsize=9)
        plt.tight_layout()
        plt.savefig(fig_path('榜单对比.png'), dpi=130)
        plt.close()
        print('✓ 榜单对比.png')

    # 图3：峰值热度分布 + 每话题评论数分布
    peaks = [t[7] for t in topics if t[7]]
    cmts = [ncmt.get(t[0], 0) for t in topics]
    if peaks:
        fig, axes = plt.subplots(1, 2, figsize=(13, 4.6))
        axes[0].hist(peaks, bins=30, color='#c0504d', alpha=0.85)
        axes[0].set_title('峰值热度分布')
        axes[0].set_xlabel('峰值热度值')
        axes[0].set_ylabel('话题数')
        axes[0].grid(alpha=0.3)
        axes[1].hist([x for x in cmts if x > 0], bins=30,
                     color='#4f81bd', alpha=0.85)
        axes[1].set_title('每话题评论数分布')
        axes[1].set_xlabel('评论数')
        axes[1].set_ylabel('话题数')
        axes[1].grid(alpha=0.3)
        plt.tight_layout()
        plt.savefig(fig_path('热度与评论分布.png'), dpi=130)
        plt.close()
        print('✓ 热度与评论分布.png')

    c.close()
    print(f'\n全部输出目录：{OUT}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
