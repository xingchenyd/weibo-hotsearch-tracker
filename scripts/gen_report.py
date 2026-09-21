# -*- coding: utf-8 -*-
"""
微博热搜生命周期追踪 —— 汇报分析生成器
生成：1 份 markdown 报告 + 5 张图表 PNG（输出到 reports/）
用 anaconda python 跑（需 matplotlib）：
    D:/anaconda/python.exe scripts/gen_report.py
"""
import os
import sqlite3
import datetime
from collections import Counter, defaultdict

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib import font_manager

# 中文字体
plt.rcParams["font.sans-serif"] = ["SimHei", "Microsoft YaHei"]
plt.rcParams["axes.unicode_minus"] = False

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB = os.path.join(ROOT, "data", "hot_tracker.db")
OUT = os.path.join(ROOT, "reports")
os.makedirs(OUT, exist_ok=True)

con = sqlite3.connect(DB)
cur = con.cursor()


def q(sql, args=()):
    cur.execute(sql, args)
    return cur.fetchall()


# ---------- 取数 ----------
events = q("""SELECT event_id, word, category, onboard_ts, offboard_ts,
                     is_active, peak_heat, latest_heat
              FROM tracked_events""")
ev_map = {r[0]: r for r in events}
n_ev = len(events)
n_active = sum(1 for r in events if r[5] == 1 or r[4] is None)
n_off = sum(1 for r in events if r[4] is not None)

posts = q("SELECT event_id, screen_name, created_at, text FROM event_posts")
n_posts = len(posts)
n_posts_ev = len(set(r[0] for r in posts))

comments = q("SELECT like_count, is_reply FROM event_comments")
n_cmt = len(comments)
n_reply = sum(1 for r in comments if r[1] == 1)
likes = [r[0] or 0 for r in comments]

hs = q("""SELECT event_id, offset_hours, heat, rank, status, lag_minutes
          FROM heat_series""")
rec = [r for r in hs if r[4] == "recorded"]
off = [r for r in hs if r[4] == "offboard"]
mis = [r for r in hs if r[4] == "missed"]
n_hs = len(hs)

req_today = None
try:
    a = sqlite3.connect(os.path.join(ROOT, "data", "safety_audit.db")).cursor()
    a.execute("SELECT COUNT(*) FROM request_audit WHERE status='request' AND ts>=?",
              ("2026-09-21",))
    req_today = a.fetchone()[0]
except Exception:
    pass

# 在榜时长（分钟）
dur = []
for r in events:
    if r[4] and r[3]:
        try:
            t0 = datetime.datetime.strptime(r[3][:19], "%Y-%m-%d %H:%M:%S")
            t1 = datetime.datetime.strptime(r[4][:19], "%Y-%m-%d %H:%M:%S")
            dur.append((r[1], (t1 - t0).total_seconds() / 60.0))
        except Exception:
            pass

cat_cnt = Counter(r[2] for r in events)
cat_have = Counter(r[0] for r in posts)

# 配图统计（统计 dataset_tracker/事件数据集/事件图/P{n}/ 下的实际文件）
PICDIR = os.path.join(ROOT, "dataset_tracker", "事件数据集", "事件图")
pic_files = 0
pic_events = 0
if os.path.isdir(PICDIR):
    for group in os.listdir(PICDIR):
        gp = os.path.join(PICDIR, group)
        if os.path.isdir(gp):
            fs = [f for f in os.listdir(gp) if os.path.isfile(os.path.join(gp, f))]
            if fs:
                pic_events += 1
                pic_files += len(fs)
# 帖子自带图（库中 pics 字段非空）的事件数
n_pics_ev = 0
try:
    cur.execute("""SELECT COUNT(DISTINCT event_id) FROM event_posts
                   WHERE pics IS NOT NULL AND pics != ''""")
    n_pics_ev = cur.fetchone()[0]
except Exception:
    pass

# ---------- 图 1：事件类型分布 ----------
top = cat_cnt.most_common(12)
fig, ax = plt.subplots(figsize=(9, 5.2))
names = [t[0] for t in top][::-1]
vals = [t[1] for t in top][::-1]
bars = ax.barh(names, vals, color="#2E5EAA")
for b, v in zip(bars, vals):
    ax.text(v + 0.2, b.get_y() + b.get_height() / 2, str(v),
            va="center", fontsize=9)
ax.set_xlabel("话题数")
ax.set_title("微博热搜话题类型分布（全部 %d 条）" % n_ev, fontsize=13)
plt.tight_layout()
p1 = os.path.join(OUT, "fig1_category.png")
plt.savefig(p1, dpi=130)
plt.close()

# ---------- 图 2：热度时点覆盖 ----------
oc = Counter(r[1] for r in rec)
offs = sorted(oc.keys())
fig, ax = plt.subplots(figsize=(9, 5.2))
ax.bar(["+%dh" % o for o in offs], [oc[o] for o in offs], color="#C0392B")
for i, o in enumerate(offs):
    ax.text(i, oc[o] + 1, str(oc[o]), ha="center", fontsize=9)
ax.set_ylabel("已记录热度点数")
ax.set_title("上热搜后各时点「已记录」热度点数量（共 %d 个）" % len(rec), fontsize=13)
plt.tight_layout()
p2 = os.path.join(OUT, "fig2_heat_offset.png")
plt.savefig(p2, dpi=130)
plt.close()

# ---------- 图 3：典型事件热度衰减曲线 ----------
by_ev = defaultdict(list)
for r in rec:
    if r[2] is not None:
        by_ev[r[0]].append((r[1], r[2]))
cand = [(eid, sorted(v)) for eid, v in by_ev.items() if len(v) >= 3]
cand.sort(key=lambda x: -ev_map[x[0]][6] if x[0] in ev_map and ev_map[x[0]][6] else 0)
cand = cand[:6]
fig, ax = plt.subplots(figsize=(9.5, 5.5))
for eid, pts in cand:
    xs = [p[0] for p in pts]
    ys = [p[1] for p in pts]
    label = ev_map[eid][1][:14] if eid in ev_map else eid[:14]
    ax.plot(xs, ys, marker="o", linewidth=2, label=label)
ax.set_xlabel("上热搜后小时数")
ax.set_ylabel("热搜热度值")
ax.set_title("典型话题热度随时间衰减曲线（前 %d 条样本最全的）" % len(cand), fontsize=13)
ax.legend(fontsize=9)
ax.grid(alpha=0.3)
plt.tight_layout()
p3 = os.path.join(OUT, "fig3_heat_decay.png")
plt.savefig(p3, dpi=130)
plt.close()

# ---------- 图 4：在榜时长分布 ----------
fig, ax = plt.subplots(figsize=(9, 5.2))
dvals = [d[1] for d in dur]
if dvals:
    ax.hist(dvals, bins=20, color="#27AE60", edgecolor="white")
    ax.axvline(sum(dvals) / len(dvals), color="#C0392B", linestyle="--",
               label="平均 %.0f 分钟" % (sum(dvals) / len(dvals)))
    ax.legend()
ax.set_xlabel("在榜时长（分钟）")
ax.set_ylabel("话题数")
ax.set_title("热搜话题「在榜时长」分布（%d 条已下榜）" % len(dvals), fontsize=13)
plt.tight_layout()
p4 = os.path.join(OUT, "fig4_duration.png")
plt.savefig(p4, dpi=130)
plt.close()

# ---------- 图 5：评论点赞分布 ----------
fig, ax = plt.subplots(figsize=(9, 5.2))
nz = [l for l in likes if l > 0]
ax.hist(nz, bins=[0, 1, 5, 10, 50, 100, 500, 1000, 6000],
        color="#8E44AD", edgecolor="white")
ax.set_xscale("symlog")
ax.set_xlabel("单条评论点赞数（对数刻度）")
ax.set_ylabel("评论数")
ax.set_title("评论点赞数分布（共 %d 条，其中 %d 条零赞）" % (n_cmt, n_cmt - len(nz)),
             fontsize=13)
plt.tight_layout()
p5 = os.path.join(OUT, "fig5_likes.png")
plt.savefig(p5, dpi=130)
plt.close()

# ---------- markdown 报告 ----------
now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
rec_offset = Counter(r[1] for r in rec)
heat_vals = [r[2] for r in rec if r[2] is not None]
md = []
md.append("# 微博热搜生命周期追踪 —— 数据汇报分析\n")
md.append("生成时间：%s\n" % now)
md.append("数据来源：微博热搜榜实时接口（`hot_band` / `side/hotSearch`），"
          "由 `scripts/hot_tracker.py` 持续采样入库（`data/hot_tracker.db`）。\n")

md.append("## 一、数据规模总览\n")
md.append("| 指标 | 数值 |\n|---|---|")
md.append("| 追踪话题总数 | %d |" % n_ev)
md.append("| 　其中仍在榜 | %d |" % n_active)
md.append("| 　其中已下榜 | %d |" % n_off)
md.append("| 已抓正文事件 | %d |" % n_posts_ev)
md.append("| 评论总数 | %d（含二级回复 %d） |" % (n_cmt, n_reply))
md.append("| 热度轨迹点 | %d（recorded %d / offboard %d / missed %d） |"
          % (n_hs, len(rec), len(off), len(mis)))
if pic_files:
    md.append("| 事件配图 | %d 个事件 / %d 张（另 %d 个事件正文微博本身无图） |"
              % (pic_events, pic_files, max(0, n_posts_ev - pic_events)))
if req_today is not None:
    md.append("| 今日接口请求 | %d / 2000 |" % req_today)
md.append("")

md.append("## 二、话题构成\n")
md.append("热搜话题天然偏向**娱乐／体育／社会民生**。类型分布（全部 %d 条）：\n" % n_ev)
md.append("| 类型 | 话题数 | 类型 | 话题数 |\n|---|---|---|---|")
tc = cat_cnt.most_common()
for i in range(0, min(12, len(tc)), 2):
    a = tc[i]
    b = tc[i + 1] if i + 1 < len(tc) else ("", "")
    md.append("| %s | %s | %s | %s |" % (a[0], a[1], b[0], b[1]))
md.append("\n![类型分布](fig1_category.png)\n")

md.append("## 三、热度轨迹分析\n")
md.append("热点话题在榜时间极短（见第五节），因此能填上的时点集中在前几小时：\n")
md.append("| 时点 | 已记录话题数 |\n|---|---|")
for o in offs:
    md.append("| 上榜后 %d 小时 | %d |" % (o, rec_offset[o]))
md.append("\n![时点覆盖](fig2_heat_offset.png)\n")
if heat_vals:
    md.append("热度值区间：**%d ~ %d**（均值 %d）。\n"
              % (min(heat_vals), max(heat_vals), sum(heat_vals) / len(heat_vals)))
md.append("\n典型话题的热度衰减曲线（能观察到「上榜即峰值、随后回落」的普遍形态）：\n")
md.append("\n![热度衰减](fig3_heat_decay.png)\n")

md.append("## 四、评论分析\n")
md.append("- 评论共 **%d** 条，其中二级回复 **%d** 条（%.1f%%）"
          % (n_cmt, n_reply, 100.0 * n_reply / max(1, n_cmt)))
if likes:
    md.append("- 单条最高点赞 **%d**，平均 **%.1f**，零赞评论 %d 条（%.0f%%）"
              % (max(likes), sum(likes) / len(likes),
                 sum(1 for l in likes if l == 0),
                 100.0 * sum(1 for l in likes if l == 0) / len(likes)))
md.append("\n![点赞分布](fig5_likes.png)\n")

md.append("## 五、话题生命周期\n")
if dur:
    md.append("在榜时长（首次观测上榜 → 最后观测在榜）：平均 **%.0f 分钟**，"
              "中位 %.0f 分钟，最长 %d 分钟（「%s」）。\n"
              % (sum(d[1] for d in dur) / len(dur),
                 sorted(d[1] for d in dur)[len(dur) // 2],
                 max(d[1] for d in dur),
                 max(dur, key=lambda x: x[1])[0]))
    md.append("\n> 这解释了为什么 24h／36h／48h／96h 等晚时点大多为空——"
              "话题往往几十分钟内就掉榜，活不到那么久。\n")
md.append("\n![在榜时长](fig4_duration.png)\n")

md.append("## 六、数据质量与口径（重要）\n")
md.append("**三种热度时点状态严格区分，只有 recorded 写值：**\n")
md.append("| 状态 | 含义 | 处理 |\n|---|---|---|")
md.append("| recorded | 目标时刻 ±35 分钟内采到样本 | 写入真实热度值 |")
md.append("| offboard | 目标时刻前话题已下榜 | 留空（本来就没有） |")
md.append("| missed | 话题仍在榜但漏采 | 留空（**绝不拿别的时刻冒充**） |")
md.append("\n**「阅读量」口径**：微博不对外提供话题阅读量接口（已试 10 个端点均无），"
          "故本表 1h~96h 列填的是**热搜榜热度值 `num`**。"
          "其数量级为数十万~百万，与模板示例「阅读量102万」量级吻合。\n")
md.append("\n**事件发生时间**：只选取**上热搜之前**发布的候选微博，"
          "保证「事件发生时间 ≤ 上热搜时间」，符合作业口径。\n")

md.append("## 七、局限与待补\n")
if pic_files:
    md.append("1. **事件配图已下载**：%d 个事件 / %d 张（存放在 `dataset_tracker/事件数据集/事件图/P{n}/`）。"
              "另有 %d 个事件的正文微博本身未配图——可加 `--supplement-search` 去话题搜索页兜底补图"
              "（每个缺图事件多 1 次需登录的搜索请求）。"
              % (pic_events, pic_files, max(0, n_posts_ev - pic_events)))
else:
    md.append("1. **事件配图未下载**（老师要求每事件 5 张）——需 `export_tracker.py --fetch-pics`。")
md.append("2. 追踪话题 %d 条中有 %d 条已抓正文，其余 %d 条待后续轮次补齐。"
          % (n_ev, n_posts_ev, n_ev - n_posts_ev))
md.append("3. 「是否为虚假」列留空（需人工判断，接口无从判定）。")
md.append("4. 采样以小时为粒度，热度时点存在 ±35 分钟容差。\n")

rp = os.path.join(OUT, "tracker_report_%s.md"
                  % datetime.datetime.now().strftime("%Y%m%d_%H%M"))
with open(rp, "w", encoding="utf-8") as f:
    f.write("\n".join(md))

con.close()
print("报告:", rp)
print("图表:", p1, p2, p3, p4, p5, sep="\n  ")
print("\n规模: 话题%d / 正文事件%d / 评论%d / 热度点%d(recorded %d)"
      % (n_ev, n_posts_ev, n_cmt, n_hs, len(rec)))
