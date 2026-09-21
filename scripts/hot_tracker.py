# -*- coding: utf-8 -*-
"""
热搜生命周期追踪器
================================================================================
目标（与原「批量事件采集」完全不同）：
  跟踪**每条热搜**从上榜到下榜的完整过程，记录：
    1. 事件最开始发生的时间      <- 该话题下最热微博的发布时间
    2. 上热搜时间               <- hot_band 接口的 onboard_time（精确时间戳）
    3. 下热搜时间               <- 我们连续观测中它最后一次出现在榜上的时刻
    4. 上热搜后 1/2/4/6/8/10/12/18/24/36/48/96 小时的**热度值**（= 浏览量口径）
    5. 评论内容 + 点赞数（不需要时间）

设计：
  · 只跟踪**新冲上榜单**的话题（保证时间线完整；已在榜的旧话题时间点无法回溯）
  · 一次 tick = 1 次热搜榜请求；正文与评论按 --max-detail 限流
  · **采样节奏（已与需求方确认）：每小时采一次榜，单位就是小时。**
    为什么不能靠"事后回补"：微博**不提供历史热搜**——date 参数被直接忽略、
    历史接口无权限（ok=-100）、无分页。所以**没采到的时点永久拿不到**，
    采样密度就是唯一的捕捉能力。选小时制的代价是时点误差约 ±30 分钟。
  · 存**原始快照**（heat_snapshots）而不是只记到点值：
    解析时点时可挑「离目标最近」的样本（它可能落在目标之前），
    把误差从"最多 +60 分钟"压到"±30 分钟"；而且某一小时漏采时，
    那一小时的快照仍在库里，后续还能继续使用。
  · 每个热度点都记录**带符号**的偏离分钟 lag_minutes，精度可核查
  · 需要更高精度时可加 `--loop-minutes`（每 --loop-interval 分钟采一次榜）

关于「浏览量」口径：
  经与需求方确认，使用**热搜热度值 heat**（即榜单条目的 num 字段）。
  话题页的"阅读 N 亿"试过 6 个接口都取不到。

⚠️ 已知限制与「如实留空」原则：
  大多数热搜条目只在榜几小时。掉榜后热度值就取不到了，
  因此 24h/36h/48h/96h 这些较晚的时点**多数会因掉榜而记不到**。
  少数长期霸榜的大事件才能填全。

  每个时点有三种状态，**只有 recorded 才写热度值，另外两种一律留空**：
    recorded —— 离目标最近的样本在 ±max_lag 分钟内，热度有效
    offboard —— 那时话题已经下榜，本来就没有热度可读
    missed   —— 话题还在榜上，是我们漏采样了（断网 / 进程没在跑 / 被限流）。
                写别的时刻的热度会误导，所以宁可留空，不伪造
  偏离分钟数（带符号，负=样本早于目标）都记在 lag_minutes 里，精度可核查。

  时间精度（小时制采样）：
    上热搜时间 —— 取自接口的 onboard_time，精确到秒（同一批上榜的词
                  共享同一时刻，因为榜单按分钟刷新），精度约 ±1 分钟。
                  **这一项不受采样频率影响，始终是最准的。**
    下热搜时间 —— 只能由"最后一次观测到在榜"推断，
                  精度 = 采样间隔（小时制约 ±1 小时）；
                  另加连续 2 次缺失才判下榜的防抖，避免榜单抖动误判
    热度时点   —— 精度约 ±30 分钟（= 采样间隔的一半）

  小时制下的两个固有损失（已知并接受）：
    · 1h/2h/4h 这几列的误差相对最大（±30 分钟对该偏移而言约 50%）
    · 生存期不足 1 小时的话题，两次采样之间它存在过，会被完全漏掉
      （onboard_time 只能补"上榜的准确时刻"，补不回"我们从未见过它"）

用法：
  python scripts/hot_tracker.py --tick                # 跑一次（定时任务用，每小时一次）
  python scripts/hot_tracker.py --tick --loop-minutes 52 --loop-interval 5   # 需要高精度时
  python scripts/hot_tracker.py --tick --no-detail    # 只记热度，不抓正文评论
  python scripts/hot_tracker.py --status              # 查看追踪状态
  python scripts/hot_tracker.py --top 60              # 只看前 N 条（默认 60）
"""
import argparse
import hashlib
import json
import os
import sqlite3
import sys
import time
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB_PATH = os.path.join(ROOT, 'data', 'hot_tracker.db')
COOKIE_FILE = os.path.join(ROOT, 'config', 'cookie.txt')
CST = timezone(timedelta(hours=8))

#: 需要记录的时效偏移点（小时）—— 必须与模板那 12 列完全一致
OFFSETS = [1, 2, 4, 6, 8, 10, 12, 18, 24, 36, 48, 96]

UA_W = ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/153.0.0.0 Safari/537.36')

DDL = """
CREATE TABLE IF NOT EXISTS tracked_events (
    event_id      TEXT PRIMARY KEY,   -- word_scheme 的稳定 hash
    word          TEXT,               -- 事件名（热搜词）
    word_scheme   TEXT,               -- #事件名#
    category      TEXT,               -- 类型（官方分类）
    onboard_ts    TEXT,               -- 上热搜时间
    offboard_ts   TEXT,               -- 下热搜时间（最后一次在榜时刻）
    first_seen_ts TEXT,               -- 我们首次观测到的时刻
    last_seen_ts  TEXT,               -- 最后一次在榜的时刻
    rank_first    INTEGER,
    rank_last     INTEGER,
    peak_heat     INTEGER,
    latest_heat   INTEGER,
    is_active     INTEGER DEFAULT 1,  -- 1=仍可能跟踪，0=已下榜
    miss_count    INTEGER DEFAULT 0,  -- 连续几次采样没在榜（用于掉榜防抖）
    subject_querys TEXT,
    url           TEXT,
    note          TEXT
);

CREATE TABLE IF NOT EXISTS heat_series (
    event_id     TEXT,
    offset_hours INTEGER,             -- 1/2/4/.../96
    target_ts    TEXT,                -- 理论上该记录的时点 = onboard + offset
    recorded_ts  TEXT,                -- 实际记录时刻
    lag_minutes  INTEGER,             -- 实际比理论晚多少分钟（15 分钟粒度下约 ±8）
    heat         INTEGER,             -- 热度值（= 浏览量口径）
    rank         INTEGER,
    status       TEXT,                -- recorded / offboard / missed
    PRIMARY KEY (event_id, offset_hours)
);

-- 榜单原始快照：每轮采样为每个在榜的跟踪话题存一行。
-- 为什么必须存原始快照：小时级采样下「上榜后 1 小时」这个目标时刻
-- 几乎不会正好落在采样点上。存下全部快照后，解析时点时可以挑
-- **离目标最近**的那个样本（它可能落在目标之前），而不是只能用
-- 目标之后的第一个样本——那会让误差固定偏大。
-- 附带好处：某一小时因断网没跑成，那一小时的快照仍在库里，下次还能用。
CREATE TABLE IF NOT EXISTS heat_snapshots (
    event_id  TEXT,
    sample_ts TEXT,                   -- 采样时刻
    heat      INTEGER,                -- 当时热度值（浏览量口径）
    rank      INTEGER,
    PRIMARY KEY (event_id, sample_ts)
);

CREATE TABLE IF NOT EXISTS event_posts (
    event_id      TEXT,
    mid           TEXT,
    created_at    TEXT,               -- 事件发生时间（微博发布时间）
    text          TEXT,               -- 事件内容
    screen_name   TEXT,               -- 来源（账号）
    followers_count INTEGER,
    comments_count  INTEGER,
    attitudes_count INTEGER,
    region_name   TEXT,
    source        TEXT,
    pics          TEXT,
    hatag_count   INTEGER,
    PRIMARY KEY (event_id, mid)
);

CREATE TABLE IF NOT EXISTS event_comments (
    event_id    TEXT,
    comment_id  TEXT,
    screen_name TEXT,
    text        TEXT,                 -- 评论内容
    like_count  INTEGER,              -- 点赞数
    is_reply    INTEGER DEFAULT 0,    -- 0=一级评论  1=二级回复（楼中楼）
    parent_comment_id TEXT,           -- 二级回复所属的一级评论
    floor_number INTEGER,             -- 楼层号
    PRIMARY KEY (event_id, comment_id)
);

CREATE TABLE IF NOT EXISTS tick_log (
    ts      TEXT,
    n_board INTEGER,
    n_new   INTEGER,
    n_off   INTEGER,
    n_heat  INTEGER,
    n_detail INTEGER,
    note    TEXT
);
CREATE INDEX IF NOT EXISTS idx_heat_ev ON heat_series(event_id);
CREATE INDEX IF NOT EXISTS idx_cm_ev ON event_comments(event_id);
"""


def now_cst():
    return datetime.now(CST)


def fmt(dt):
    return dt.strftime('%Y-%m-%d %H:%M:%S')


def eid_of(word_scheme):
    """用 word_scheme 生成稳定 event_id（避免中文/特殊字符问题）。"""
    return hashlib.md5((word_scheme or '').encode('utf-8')).hexdigest()[:16]


def conn():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    c = sqlite3.connect(DB_PATH)
    c.executescript(DDL)
    return c


def fetch_board(limit):
    """取热搜榜前 limit 条。无需 Cookie。"""
    from curl_cffi import requests as creq
    r = creq.get('https://weibo.com/ajax/statuses/hot_band?band_id=1',
                 headers={'User-Agent': UA_W, 'Referer': 'https://weibo.com/',
                          'Accept': 'application/json, text/plain, */*'},
                 impersonate='chrome', timeout=25)
    d = r.json()
    lst = ((d.get('data') or {}).get('band_list')) or []
    if not lst:                                  # 退化到热搜总榜
        d2 = creq.get('https://weibo.com/ajax/side/hotSearch',
                      headers={'User-Agent': UA_W, 'Referer': 'https://weibo.com/'},
                      impersonate='chrome', timeout=25).json()
        lst = ((d2.get('data') or {}).get('realtime')) or []
    out = []
    for it in lst[:limit]:
        if not it.get('word'):
            continue
        if it.get('is_ad'):
            continue
        out.append({
            'word': it.get('word'),
            'word_scheme': it.get('word_scheme') or f"#{it.get('word')}#",
            'category': it.get('category') or '',
            'heat': it.get('num'),
            'rank': (it.get('realpos') or it.get('rank') or 0) or 0,
            'onboard_time': it.get('onboard_time'),
            'subject_querys': it.get('subject_querys') or '',
            'url': it.get('url') or '',
        })
    return out


def migrate(c):
    """给已存在的表补字段（SQLite 的 ALTER TABLE 幂等封装）。"""
    def cols(t):
        return {r[1] for r in c.execute(f'PRAGMA table_info({t})')}
    want = {
        'tracked_events': [('miss_count', 'INTEGER DEFAULT 0')],
        'event_comments': [('is_reply', 'INTEGER DEFAULT 0'),
                           ('parent_comment_id', 'TEXT'),
                           ('floor_number', 'INTEGER')],
    }
    for t, items in want.items():
        have = cols(t)
        for name, decl in items:
            if name not in have:
                c.execute(f'ALTER TABLE {t} ADD COLUMN {name} {decl}')
    c.commit()


def tick(top=60, with_detail=True, verbose=True, comment_pages=3,
         max_detail=3, max_post_age=6, max_lag=35, offboard_misses=2,
         min_gap=30):
    """跑一次。

    max_detail 限制本次最多为几条话题抓正文与评论。
    为什么需要：热搜每小时能新增 ~20 条，每条抓正文+评论约 5 次请求，
    不设上限的话一天会到 2600+ 次请求，超出账号安全额度。
    热度时间线（board 接口）很便宜，所以跟踪面可以广；
    正文与评论贵，必须有预算控制。

    max_post_age：只给最近这么多小时内上榜的话题补正文（默认 6 小时）。
    超过这个窗口就不补了——它已经不是"正在发生的事件"。

    max_lag：热度时点与最近样本之间允许的最大偏差（分钟，默认 35）。
    每小时采样一次时，目标时刻（上榜时刻 + 偏移）几乎不会正好落在
    采样点上，偏差天然可达 ±30 分钟，所以阈值必须 ≥30。
    超过阈值就留空并标记 missed/offboard，**不写别的时刻的热度冒充**。
    注意 lag_minutes 是**带符号**的（负 = 样本早于目标），可据此核查精度。
    如果改成高频采样（如 --loop-interval 5），可以把阈值收到 10~15。

    offboard_misses：连续几次采样不在榜才判定"下热搜"（默认 2）。
    单次看不到不算——热搜榜会抖动，话题可能掉出前 50 又回来。
    """
    c = conn()
    migrate(c)
    now = now_cst()
    now_s = fmt(now)

    # 防重复轮次：定时任务延迟会让多个会话排队，可能在同一小时里各采一遍，
    # 结果就是重复抓同一批话题、请求量翻倍。这里用"最近一次采样的时刻"兜底：
    # 距现在不足 min_gap 分钟就说明这一小时已经采过了，直接跳过。
    if min_gap > 0:
        last = c.execute('SELECT MAX(sample_ts) FROM heat_snapshots').fetchone()
        if last and last[0]:
            try:
                lt = datetime.strptime(last[0], '%Y-%m-%d %H:%M:%S').replace(
                    tzinfo=CST)
                gap = (now - lt).total_seconds() / 60
            except ValueError:
                gap = None
            if gap is not None and gap < min_gap:
                if verbose:
                    print(f'  最近一次采样在 {last[0][11:]}（{gap:.0f} 分钟前），'
                          f'不足 {min_gap} 分钟 → 本轮跳过，避免重复采集')
                c.close()
                return 0

    items = fetch_board(top)
    board_ids = {}
    for it in items:
        board_ids[eid_of(it['word_scheme'])] = it
    if verbose:
        print(f'[{now_s}] 热搜榜取到 {len(items)} 条（广告已剔除）')

    new_ids, n_heat, n_miss, n_skip_old = [], 0, 0, 0

    # ---- 1) 更新 / 新增跟踪对象 ----
    for eid, it in board_ids.items():
        ob = it.get('onboard_time')
        ob_s = ''
        if isinstance(ob, (int, float)) and ob > 0:
            ob_s = fmt(datetime.fromtimestamp(ob, CST))
        row = c.execute('SELECT event_id, is_active, rank_first FROM tracked_events '
                        'WHERE event_id=?', (eid,)).fetchone()
        if row is None:
            # 只跟踪"新上榜"的：要求 onboard_time 距现在不超过 40 分钟。
            # 已在榜很久的话题，前面的时点（1h/2h/…）已经过去、无法回溯，
            # 纳入只会带来时间线缺口，所以直接跳过。
            if not ob_s:
                n_skip_old += 1
                continue
            age_min = (now - datetime.strptime(ob_s, '%Y-%m-%d %H:%M:%S')
                       .replace(tzinfo=CST)).total_seconds() / 60
            if age_min > 40:
                n_skip_old += 1
                continue
            c.execute("""INSERT INTO tracked_events
                (event_id, word, word_scheme, category, onboard_ts, first_seen_ts,
                 last_seen_ts, rank_first, rank_last, peak_heat, latest_heat,
                 is_active, subject_querys, url, note)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,1,?,?,?)""",
                (eid, it['word'], it['word_scheme'], it['category'],
                 ob_s, now_s, now_s, it['rank'], it['rank'],
                 it['heat'], it['heat'], it['subject_querys'], it['url'],
                 f'上榜 {int(age_min)} 分钟后纳入'))
            new_ids.append(eid)
        else:
            prev = c.execute('SELECT is_active FROM tracked_events '
                             'WHERE event_id=?', (eid,)).fetchone()
            came_back = bool(prev) and not prev[0]
            c.execute("""UPDATE tracked_events SET last_seen_ts=?, rank_last=?,
                         latest_heat=?, is_active=1, offboard_ts=NULL,
                         peak_heat=MAX(COALESCE(peak_heat,0), COALESCE(?,0))
                         WHERE event_id=?""",
                      (now_s, it['rank'], it['heat'], it['heat'], eid))
            if came_back:
                # 曾判定下榜、现在又回到榜上：清掉下热搜时间（它当前在榜），
                # 并在 note 里留言，避免看起来像"从未下榜"
                c.execute("UPDATE tracked_events SET note=COALESCE(note,'') || ? "
                          "WHERE event_id=?", (f' | 回榜@{now_s[11:16]}', eid))
    if verbose and n_skip_old:
        print(f'  跳过 {n_skip_old} 条"早已上榜"的（时间线无法完整，按你的要求不跟踪）')

    # ---- 1.5) 存本轮榜单快照 ----
    # 每个在榜的跟踪话题存一行 (event_id, sample_ts, heat, rank)。
    # 每行只有几十字节，换来两个好处：
    #   1) 解析时效时点时可挑「离目标最近」的样本（可在目标之前），
    #      而不是只能用目标之后的第一个——那会让误差固定偏大
    #   2) 某一轮因断网没跑成，那一轮的快照仍在库里，后面还能使用
    n_snap = 0
    tracked_ids = {r[0] for r in c.execute('SELECT event_id FROM tracked_events')}
    for eid, it in board_ids.items():
        if eid not in tracked_ids:
            continue
        c.execute("""INSERT OR REPLACE INTO heat_snapshots
                     (event_id, sample_ts, heat, rank) VALUES (?,?,?,?)""",
                  (eid, now_s, it['heat'], it['rank']))
        n_snap += 1
    # 立刻落库。踩过的坑：所有写入原来都在 tick 末尾统一 commit，
    # 一旦进程被杀（本机确实会回收后台进程），**整轮工作全部回滚**——
    # 实测 18:44 那轮跑了 7 分钟、发了几十次请求，被 kill 后库里一条都没留下。
    c.commit()

    # ---- 2) 掉榜检测（带防抖）----
    # 单次"看不到"不算下榜：热搜榜会短暂抖动，话题可能掉出前 50 又回来。
    # 必须连续 offboard_misses 次采样都不在榜才判定下榜，
    # 并把「最后一次在榜的时刻」作为下热搜时间。
    n_off = 0
    for eid, last_seen, miss in c.execute(
            'SELECT event_id, last_seen_ts, COALESCE(miss_count,0) '
            'FROM tracked_events WHERE is_active=1'):
        if eid in board_ids:
            if miss:
                c.execute('UPDATE tracked_events SET miss_count=0 '
                          'WHERE event_id=?', (eid,))
            continue
        miss += 1
        if miss < offboard_misses:
            c.execute('UPDATE tracked_events SET miss_count=? WHERE event_id=?',
                      (miss, eid))
            continue
        c.execute('UPDATE tracked_events SET is_active=0, offboard_ts=?, '
                  'miss_count=? WHERE event_id=?', (last_seen, miss, eid))
        n_off += 1

    # ---- 3) 到点的时效偏移，记录热度值 ----
    for eid, ob, act, off_ts in c.execute(
            'SELECT event_id, onboard_ts, is_active, offboard_ts '
            'FROM tracked_events'):
        if not ob:
            continue
        try:
            ob_dt = datetime.strptime(ob, '%Y-%m-%d %H:%M:%S').replace(tzinfo=CST)
        except ValueError:
            continue
        done = {r[0] for r in c.execute(
            'SELECT offset_hours FROM heat_series WHERE event_id=?', (eid,))}
        for off in OFFSETS:
            if off in done:
                continue
            target = ob_dt + timedelta(hours=off)
            if now < target:
                continue
            # 在所有快照里挑「离目标最近」的样本（它可能落在目标之前）。
            # 小时级采样下这一步很关键：目标时刻几乎不会正好落在采样点上，
            # 若只取目标之后的第一个样本，误差会固定偏大最多一个采样间隔。
            best = None
            for sts, heat, rk in c.execute(
                    'SELECT sample_ts, heat, rank FROM heat_snapshots '
                    'WHERE event_id=?', (eid,)):
                st = datetime.strptime(sts, '%Y-%m-%d %H:%M:%S').replace(tzinfo=CST)
                d = int((st - target).total_seconds() // 60)
                if best is None or abs(d) < abs(best[0]):
                    best = (d, sts, heat, rk)
            if best is None:
                # 从未采到过它的热度 → 无值可记
                c.execute("""INSERT OR REPLACE INTO heat_series
                    (event_id, offset_hours, target_ts, recorded_ts, lag_minutes,
                     heat, rank, status) VALUES (?,?,?,?,NULL,NULL,NULL,'offboard')""",
                    (eid, off, fmt(target), now_s))
                continue
            lag, sts, heat, rk = best
            if abs(lag) > max_lag:
                # 最近的样本离目标也太远。这时要区分两种原因，别混为一谈：
                #   该话题在目标时刻之前就已经下榜 → offboard（本来就没有值）
                #   话题还在榜上，是我们漏采了      → missed（本可以取到却错过）
                left_before = bool(off_ts) and off_ts <= fmt(target)
                # 注意：变量名不能叫 status —— 会遮蔽模块级的 status() 函数
                pt_status = 'offboard' if left_before else 'missed'
                c.execute("""INSERT OR REPLACE INTO heat_series
                    (event_id, offset_hours, target_ts, recorded_ts, lag_minutes,
                     heat, rank, status) VALUES (?,?,?,?,?,NULL,NULL,?)""",
                    (eid, off, fmt(target), now_s, lag, pt_status))
                if pt_status == 'missed':
                    n_miss += 1
                continue
            c.execute("""INSERT OR REPLACE INTO heat_series
                (event_id, offset_hours, target_ts, recorded_ts, lag_minutes,
                 heat, rank, status) VALUES (?,?,?,?,?,?,?,'recorded')""",
                (eid, off, fmt(target), sts, lag, heat, rk))
            n_heat += 1
    c.commit()          # 热度时点立刻落库，别等 tick 末尾（进程可能被杀）

    # ---- 4) 为还没抓到正文的话题抓正文与评论（可选）----
    n_detail = 0
    if with_detail:
        # 从库里挑"已追踪但还没抓到正文"的话题，而不是只看本轮新增的：
        # 这样上一轮因 --max-detail 限流、或进程被中断而漏掉的，
        # 后面几轮会自动补上，不会永久缺失。
        # 只补最近 max_post_age 小时内上榜的，避免去追已经不新鲜的话题。
        cutoff = fmt(now - timedelta(hours=max_post_age))
        pending = c.execute("""
            SELECT e.event_id, e.word, e.onboard_ts
            FROM tracked_events e
            WHERE e.onboard_ts >= ?
              AND NOT EXISTS (SELECT 1 FROM event_posts p
                              WHERE p.event_id = e.event_id)
            ORDER BY COALESCE(e.rank_first, 999)""", (cutoff,)).fetchall()
        # todo 带上 onboard_ts：取正文时要优先挑"上榜之前发布"的微博，
        # 否则会把上热搜之后才发的帖子当成事件正文，导致发生时间倒挂
        todo = [(r[0], r[2]) for r in pending[:max_detail]]
        if verbose and len(pending) > len(todo):
            print(f'  待抓正文 {len(pending)} 条，本轮处理前 {len(todo)} 条'
                  f'（--max-detail {max_detail} 限流）')
        if verbose and not pending:
            print('  无待抓正文的话题')
        from event_crawler import (Fetcher, SafetyGuard, discover_search,
                                   fetch_post, fetch_comments, Blocked,
                                   SafetyAbort)
        cookie = ''
        if os.path.exists(COOKIE_FILE):
            cookie = open(COOKIE_FILE, encoding='utf-8').read().strip()
        # 单轮上限必须随 max_detail 放大，否则会被自己熔断：
        # 每条详情约 5 次请求（搜索 1 + 正文 1 + 评论 3 页），
        # 12 条就是 60 次，正好撞上原来写死的 60。
        guard = SafetyGuard(os.path.join(ROOT, 'data', 'safety_audit.db'),
                            max_per_run=max(60, max_detail * 6 + 10),
                            max_per_day=2000,
                            min_delay=10, max_delay=20, verbose=True,
                            max_consecutive_search=8)
        f = Fetcher(cookie=cookie, guard=guard, verbose=True)
        for eid, ob_ts in todo:
            row = c.execute('SELECT word, word_scheme FROM tracked_events WHERE event_id=?',
                            (eid,)).fetchone()
            if not row:
                continue
            word = row[0]
            try:
                # 优先挑"上热搜之前发布"的微博当事件正文，
                # 避免把上榜之后才发的帖子选出来（会让发生时间晚于上榜时间）
                d_stats = {}
                got = discover_search(f, word, 1, pages=1, min_comments=5,
                                      max_age_hours=72, stats=d_stats,
                                      prefer_before=ob_ts)
                if not got:
                    if verbose:
                        print(f'    「{word}」无合适微博，跳过正文')
                    continue
                mid = got[0][0]
                post = fetch_post(f, mid)
                if not post:
                    continue
                c.execute("""INSERT OR REPLACE INTO event_posts
                    (event_id, mid, created_at, text, screen_name,
                     followers_count, comments_count, attitudes_count,
                     region_name, source, pics, hatag_count)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (eid, mid, post.get('created_at'), post.get('text'),
                     post.get('screen_name'), post.get('followers_count'),
                     post.get('comments_count'), post.get('attitudes_count'),
                     post.get('region_name'), post.get('source'),
                     post.get('pics'), post.get('hashtag_count')))
                cmts, total = fetch_comments(f, mid, comment_pages)
                for cm in cmts:
                    c.execute("""INSERT OR REPLACE INTO event_comments
                        (event_id, comment_id, screen_name, text, like_count,
                         is_reply, parent_comment_id, floor_number)
                        VALUES (?,?,?,?,?,?,?,?)""",
                        (eid, cm.get('comment_id'), cm.get('screen_name'),
                         cm.get('text'), cm.get('like_count'),
                         cm.get('is_reply') or 0,
                         str(cm.get('parent_comment_id') or ''),
                         cm.get('floor_number')))
                n1 = sum(1 for x in cmts if not x.get('is_reply'))
                n2 = len(cmts) - n1
                if d_stats.get('before_pref_failed'):
                    # 上榜前没有任何合格候选，只能退回全局最热 —— 这条的
                    # 「事件发生时间」可能晚于「上热搜时间」，必须留痕，
                    # 不能悄悄给一个时间倒挂的值
                    c.execute("UPDATE tracked_events SET note="
                              "COALESCE(note,'') || ? WHERE event_id=?",
                              (' | 正文时间晚于上榜(上榜前无候选)', eid))
                    if verbose:
                        print('      ⚠ 上榜前没有合格候选，回退到全局最热，已标记')
                # 每条详情抓完立刻落库：这样即使后续条目中途被杀，
                # 已经花掉的请求不会白费（正文 + 它的评论一起提交）
                c.commit()
                n_detail += 1
                if verbose:
                    print(f'    「{word}」正文已记录；评论 {len(cmts)} 条'
                          f'（一级 {n1} + 二级回复 {n2}，按热度优先）')
            except SafetyAbort as exc:
                print(f'    ⛔ 安全熔断，停止抓取正文：{exc}')
                break
            except Blocked as exc:
                if verbose:
                    print(f'    「{word}」抓取受阻：{exc}')
        print('    ' + guard.summary())

    c.execute('INSERT INTO tick_log VALUES (?,?,?,?,?,?,?)',
              (now_s, len(items), len(new_ids), n_off, n_heat, n_detail, ''))
    c.commit()

    st = status(c, quiet=True)
    if verbose:
        print(f'  本次：新上榜 {len(new_ids)} · 掉榜 {n_off} · '
              f'记录热度 {n_heat} · 漏采(超时留空) {n_miss} · 抓详情 {n_detail}')
        print(f'  追踪中 {st["active"]} 条 · 已下榜 {st["offboard"]} 条 · '
              f'热度点 {st["series"]} 个 · 评论 {st["comments"]} 条')
    c.close()
    return 0


def status(c=None, quiet=False):
    own = c is None
    if own:
        c = conn()
    active = c.execute('SELECT COUNT(*) FROM tracked_events WHERE is_active=1').fetchone()[0]
    off = c.execute('SELECT COUNT(*) FROM tracked_events WHERE is_active=0').fetchone()[0]
    series = c.execute('SELECT COUNT(*) FROM heat_series').fetchone()[0]
    rec = c.execute("SELECT COUNT(*) FROM heat_series WHERE status='recorded'").fetchone()[0]
    cm = c.execute('SELECT COUNT(*) FROM event_comments').fetchone()[0]
    posts = c.execute('SELECT COUNT(*) FROM event_posts').fetchone()[0]
    out = {'active': active, 'offboard': off, 'series': series,
           'recorded': rec, 'comments': cm, 'posts': posts}
    if own and not quiet:
        print('=' * 66)
        print('热搜追踪状态')
        print('=' * 66)
        print(f'  追踪中（仍在榜）: {active}')
        print(f'  已下榜:          {off}')
        print(f'  热度记录点:       {series}（其中有效 {rec}）')
        print(f'  已抓正文:         {posts}')
        print(f'  已抓评论:         {cm}')
        print()
        print('  最近 10 条追踪对象:')
        for r in c.execute("""SELECT word, category, onboard_ts, offboard_ts,
                                     latest_heat, is_active
                              FROM tracked_events ORDER BY first_seen_ts DESC
                              LIMIT 10"""):
            st_ = '在榜' if r[5] else '下榜'
            print(f'    [{st_}] {(r[0] or "")[:24]:26s} {(r[1] or "")[:8]:10s} '
                  f'上={r[2][5:16] if r[2] else "-"} 下={(r[3][5:16] if r[3] else "-"):11s} '
                  f'热度={r[4] or 0:>9,}')
        print()
        print('  热度轨迹示例（前 2 条有多点时点的）:')
        for eid, word in c.execute("""SELECT event_id, word FROM tracked_events
                                     WHERE event_id IN (SELECT event_id FROM heat_series
                                                        GROUP BY event_id
                                                        HAVING COUNT(*) >= 3)
                                     LIMIT 2"""):
            print(f'    「{word}」')
            for r in c.execute("""SELECT offset_hours, heat, rank, status, lag_minutes
                                  FROM heat_series WHERE event_id=?
                                  ORDER BY offset_hours""", (eid,)):
                h = f'{r[1]:,}' if r[1] else '—'
                print(f'      +{r[2] if False else r[0]:>2}h  热度 {h:>10}  '
                      f'排名 {r[2] if r[2] else "-":>3}  {r[3]}  滞后{r[4]}分')
    if own:
        c.close()
    return out


def wait_for_next_hour(max_wait_min=60):
    """等下一个整点再开始采样。返回实际等待秒数。

    为什么需要：定时任务的 RRULE 本身是**绝对整点触发**，但触发器响了之后，
    这个会话什么时候真正开始跑由宿主排队决定——实测延迟在累积：

        16:09:56 触发 → 16:10:26 启动（+0.5 分）
        17:09:56 触发 → 17:20:56 启动（+12 分）
        18:09:56 触发 → 18:49:34 启动（+40 分）

    排队行为改不了，但**数据质量只取决于「采样发生在几点」，不取决于
    「会话几点启动」**。所以在会话里先等到整点再采，就能让每个小时的热度
    值都精确落在整点附近，与触发延迟无关。

    上限 max_wait_min：等太久反而容易被环境回收。超过就放弃等待、立即开始，
    并明确打印出来（不静默降级）。
    """
    now = datetime.now(CST)
    nxt = now.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
    delta = (nxt - now).total_seconds()
    if delta < 20:
        print(f'   现在 {now:%H:%M:%S}，已在整点附近，直接开始采样')
        return 0.0
    if delta > max_wait_min * 60:
        print(f'   现在 {now:%H:%M:%S}，距下个整点还有 {delta/60:.0f} 分钟，'
              f'超过等待上限 {max_wait_min} 分钟 → 不等待，立即开始'
              f'（本小时的采样时点会偏离整点）')
        return 0.0
    print(f'   现在 {now:%H:%M:%S}，为对齐整点休眠 {delta/60:.1f} 分钟'
          f'（将在 {nxt:%H:%M} 采样）', flush=True)
    t0 = time.time()
    try:
        time.sleep(delta)
    except KeyboardInterrupt:
        pass
    print(f'   已到 {datetime.now(CST):%H:%M:%S}，开始采样', flush=True)
    return time.time() - t0


def run_loop(args):
    """循环模式：在一个进程里持续采样，提升时效时点的精度。

    定时任务的最小周期是 1 小时。若只靠它每小时采一次榜，
    "上热搜后 1 小时"这个点会带最多 60 分钟的滞后，1h/2h 的读数就失真了。
    本模式每 loop_interval 分钟采一次榜（每次只 1 次请求），
    把时点误差压到 ±loop_interval 分钟。

    正文与评论较贵（每条约 5 次请求），所以默认每小时才抓一次
    （detail_every），与原来的行为一致，不会显著增加请求量。
    """
    from safety import SafetyAbort

    interval = max(1, args.loop_interval)
    detail_every = (args.detail_every if args.detail_every > 0
                    else max(1, round(60 / interval)))
    deadline = time.time() + args.loop_minutes * 60
    rounds = 0
    while True:
        rounds += 1
        do_detail = (not args.no_detail) and (rounds % detail_every == 1)
        left = max(0, int((deadline - time.time()) / 60))
        print(f'--- 循环第 {rounds} 轮（抓正文评论：{"是" if do_detail else "否"}'
              f'，剩余约 {left} 分钟）---', flush=True)
        try:
            tick(top=args.top, with_detail=do_detail,
                 comment_pages=args.comment_pages,
                 max_detail=args.max_detail,
                 max_lag=args.max_lag_minutes,
                 offboard_misses=args.offboard_misses,
                 min_gap=0)      # 循环模式本来就要高频采样，不能去重
        except KeyboardInterrupt:
            print('  收到中断，退出循环', flush=True)
            return 0
        except Exception as exc:                     # noqa: BLE001
            # 安全熔断必须真的停下；其他单轮异常不应让循环退出
            # （否则又会造成一次"误停"，这是之前踩过的坑）
            if isinstance(exc, SafetyAbort):
                print(f'  ⛔ 安全熔断，循环终止：{str(exc)[:160]}', flush=True)
                return 1
            print(f'  ⚠ 本轮异常 {type(exc).__name__}: {str(exc)[:110]}'
                  f'（继续下一轮）', flush=True)
        if time.time() >= deadline:
            break
        # 只睡到 deadline 为止，避免最后一轮超出用户指定的时长
        nap = min(interval * 60, max(0.0, deadline - time.time()))
        if nap < 1:
            break
        print(f'  休眠 {nap / 60:.1f} 分钟后继续', flush=True)
        try:
            time.sleep(nap)
        except KeyboardInterrupt:
            return 0
    print(f'循环结束，共 {rounds} 轮', flush=True)
    return 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--tick', action='store_true', help='跑一次（定时任务用）')
    ap.add_argument('--status', action='store_true', help='查看追踪状态')
    ap.add_argument('--top', type=int, default=60, help='只跟踪前 N 条（默认 60）')
    ap.add_argument('--no-detail', action='store_true',
                    help='只记热度，不抓正文评论（更省请求）')
    ap.add_argument('--comment-pages', type=int, default=3,
                    help='每条微博抓几页评论（默认 3）。接口按热度返回，'
                         '会同时收录一级评论与二级回复')
    ap.add_argument('--max-detail', type=int, default=3,
                    help='本次最多为几条新话题抓正文评论（默认 3）。'
                         '不设上限会撑爆请求额度')
    ap.add_argument('--loop-minutes', type=int, default=0,
                    help='循环模式：本进程持续运行 N 分钟，每 --loop-interval '
                         '分钟采一次热搜榜。用来把"上榜后 1h/2h"等时点的误差'
                         '从最多 60 分钟压到 ±interval（默认 0 = 只跑一次）')
    ap.add_argument('--loop-interval', type=int, default=5,
                    help='循环模式下的采样间隔，分钟（默认 5）')
    ap.add_argument('--detail-every', type=int, default=0,
                    help='循环模式下每几轮抓一次正文评论（默认按间隔自动推算，'
                         '使正文抓取约每小时一次）')
    ap.add_argument('--max-lag-minutes', type=int, default=35,
                    help='热度时点与最近样本允许的最大偏差（默认 35 分钟，'
                         '适配每小时采样：目标时刻与采样点天然可差 ±30 分钟）。'
                         '超过就标记 missed/offboard 并留空，不拿别的时刻的热度'
                         '冒充。改成高频采样时应收紧到 10~15')
    ap.add_argument('--offboard-misses', type=int, default=2,
                    help='连续几次采样不在榜才判定下热搜（默认 2，防榜单抖动）')
    ap.add_argument('--align-hour', action='store_true',
                    help='先等到下一个整点再采样。用来抵消定时任务的启动延迟'
                         '（实测延迟会累加到 40 分钟），让每小时的热度值'
                         '落在同一个相位上')
    ap.add_argument('--max-wait-min', type=int, default=60,
                    help='--align-hour 的最长等待分钟数（默认 60，超过就立即开始）')
    ap.add_argument('--min-gap-min', type=int, default=30,
                    help='若最近一次采样距今不足这么多分钟，本轮直接跳过'
                         '（默认 30，防止排队重复运行导致请求翻倍）')
    args = ap.parse_args()

    if args.status:
        status()
        return 0
    if not args.tick:
        ap.print_help()
        return 0
    if args.align_hour:
        wait_for_next_hour(args.max_wait_min)
    if args.loop_minutes > 0:
        return run_loop(args)
    return tick(top=args.top, with_detail=not args.no_detail,
                comment_pages=args.comment_pages,
                max_detail=args.max_detail,
                max_lag=args.max_lag_minutes,
                offboard_misses=args.offboard_misses,
                min_gap=args.min_gap_min)


if __name__ == '__main__':
    sys.exit(main())
