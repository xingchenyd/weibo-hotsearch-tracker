# -*- coding: utf-8 -*-
"""
探测工具：分类榜接口 + 评论游标翻页可行性（含一次"负结果"记录）
================================================================================
⚠️ 结论先行（重要）：本脚本 A 段枚举 `hot_band?band_id=0..25` 的做法**已被证伪**——
   band_id 参数被服务端**完全忽略**（0~25 全部返回同一份总榜，响应签名一致）。
   真正的六个分类榜是独立路径，**不是** hot_band 的参数：

     文娱 /ajax/statuses/entertainment   生活 /ajax/statuses/life
     社会 /ajax/statuses/social          体育 /ajax/statuses/sport   ← 单数，写 sports 会 404
     科技 /ajax/statuses/technology      ACG  /ajax/statuses/acg
     且全部**必须带登录 Cookie**（无 Cookie 时 HTTP 200 但 band_list 为空，且不报错）

   该结论已固化进 `multi_board_tracker.py`（BOARD_ENDPOINTS 常量）。
   本脚本保留 A 段，是为了把"为什么不能用 band_id"这一段排查过程留档。

B 段（评论游标翻页）仍是有效工具：验证 hotflow 的 max_id 能否稳定前进，
   这是"评论逐页累积去重"方案能否成立的前提。

用法：python -u scripts/probe_bands.py
输出：控制台可直接读取（会明示"结论"行）；不写任何数据库。
"""
import json
import os
import sqlite3
import sys
import time

from curl_cffi import requests as creq

UA_W = ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36')
HDR = {'User-Agent': UA_W, 'Referer': 'https://weibo.com/',
       'Accept': 'application/json, text/plain, */*'}
UA_M = ('Mozilla/5.0 (iPhone; CPU iPhone OS 16_6 like Mac OS X) '
        'AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.6 Mobile/15E148 Safari/604.1')
HDR_M = {'User-Agent': UA_M, 'Referer': 'https://m.weibo.cn/',
         'Accept': 'application/json, text/plain, */*',
         'X-Requested-With': 'XMLHttpRequest'}

# B 段需要一个已抓过的 mid。两个库的表名不同，依次尝试。
DB_CANDIDATES = ['data/multi_boards.db', 'data/hot_tracker.db']


def probe_bands():
    """A. 枚举 band_id，找六个分类榜。"""
    print('=' * 78)
    print('A. hot_band band_id 0..25 探测（找六个分类榜）')
    print('=' * 78)
    ok = []
    for bid in range(0, 26):
        try:
            r = creq.get(f'https://weibo.com/ajax/statuses/hot_band?band_id={bid}',
                         headers=HDR, impersonate='chrome', timeout=15)
            if r.status_code != 200:
                print(f'  band_id={bid:>2}: HTTP {r.status_code}')
                continue
            d = r.json()
            data = d.get('data') or {}
            lst = data.get('band_list') or (data.get('realtime') or [])
            # 收集所有字符串型元字段（榜名可能在这里）
            meta = {k: v for k, v in data.items()
                    if isinstance(v, str) and len(v) < 40}
            # 条目里的分类字段
            cats = set()
            for it in lst[:50]:
                c = it.get('category') or it.get('cate') or it.get('label_name')
                if c:
                    cats.add(c)
            print(f'  band_id={bid:>2}: 条数={len(lst):>3}  元字段={meta}')
            if lst:
                words = [it.get('word') for it in lst[:6]]
                print(f'            前6词: {words}')
                if cats:
                    print(f'            条目 category: {sorted(cats)[:8]}')
                ok.append((bid, len(lst), meta))
        except Exception as e:                                    # noqa: BLE001
            print(f'  band_id={bid:>2}: ERR {type(e).__name__}: {str(e)[:60]}')
        time.sleep(1.2)

    print('\n  【结论 A】有数据的 band_id / 条数:',
          [(b, n) for b, n, _ in ok])
    # 打印一个非 1 的榜的完整首条，便于找榜名字段
    for bid, n, _ in ok:
        if bid != 1:
            print(f'\n  band_id={bid} 的首条条目（找榜名/分类字段）:')
            try:
                d = creq.get(f'https://weibo.com/ajax/statuses/hot_band?band_id={bid}',
                             headers=HDR, impersonate='chrome', timeout=15).json()
                data = d.get('data') or {}
                print('    data 顶层键:', list(data.keys()))
                lst = data.get('band_list') or []
                if lst:
                    print('    首条键:', list(lst[0].keys()))
                    print('    首条:', json.dumps(lst[0], ensure_ascii=False)[:500])
            except Exception as e:                                # noqa: BLE001
                print('    失败:', e)
            break
    return ok


def _pick_mid():
    """挑一条评论数较多的微博 mid，用于评论翻页测试。

    两代库的表名不同（multi_boards → topic_posts / hot_tracker → event_posts），
    依次尝试；都没有就返回 None，调用方会跳过 B 段（不算失败）。
    """
    for db in DB_CANDIDATES:
        if not os.path.exists(db):
            continue
        con = sqlite3.connect(db)
        try:
            tables = {r[0] for r in con.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")}
            for t in ('topic_posts', 'event_posts'):
                if t not in tables:
                    continue
                row = con.execute(
                    f"SELECT mid FROM {t} WHERE mid IS NOT NULL AND mid != '' "
                    f"ORDER BY COALESCE(comments_count, 0) DESC LIMIT 1").fetchone()
                if row:
                    return row[0], f'{db}:{t}'
        except sqlite3.Error:
            pass
        finally:
            con.close()
    return None, None


def probe_comment_cursor():
    """B. 测 hotflow 游标翻页。"""
    print('\n' + '=' * 78)
    print('B. 评论游标翻页测试（hotflow 连续翻 3 页）')
    print('=' * 78)
    mid, src = _pick_mid()
    if not mid:
        print('  本地库没找到可用 mid，跳过 B 段。'
              '（可先跑一轮 multi_board_tracker.py --tick 生成数据）')
        return
    print(f'  取样微博 mid={mid}（来源：{src}）')

    seen = set()
    max_id = None
    max_id_type = 0
    for page in range(1, 4):
        url = f'https://m.weibo.cn/comments/hotflow?id={mid}&mid={mid}&max_id_type={max_id_type}'
        if max_id:
            url += f'&max_id={max_id}'
        try:
            r = creq.get(url, headers=HDR_M, impersonate='chrome', timeout=20)
            d = r.json()
        except Exception as e:                                    # noqa: BLE001
            print(f'  第{page}页: ERR {type(e).__name__}: {str(e)[:70]}')
            return
        data = d.get('data') or {}
        lst = data.get('data') or []
        new_max = data.get('max_id')
        new_type = data.get('max_id_type')
        ids = [str(it.get('id')) for it in lst]
        dup = len(set(ids) & seen)
        seen |= set(ids)
        print(f'  第{page}页: HTTP {r.status_code}  ok={d.get("ok")}  条数={len(lst):>3}  '
              f'本页重复={dup:>3}  max_id={new_max}  max_id_type={new_type}')
        if lst:
            print(f'           首条: {str(lst[0].get("text"))[:40]!r} 赞{lst[0].get("like_count")}')
        if not lst:
            print('           → 该页为空，说明已翻到底（exhausted）')
            break
        if str(new_max) in ('0', 'None', '', None):
            print('           → max_id 归零，已到底')
            break
        max_id, max_id_type = new_max, (new_type if new_type is not None else max_id_type)
        time.sleep(2)

    print(f'\n  【结论 B】累计去重后评论 {len(seen)} 条；'
          f'{"游标可翻页 ✅" if len(seen) > 20 else "翻页可能无效 ⚠️"}')


def main():
    probe_bands()
    probe_comment_cursor()
    print('\n完成。')
    return 0


if __name__ == '__main__':
    sys.exit(main())
