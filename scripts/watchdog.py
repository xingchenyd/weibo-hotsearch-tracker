# -*- coding: utf-8 -*-
"""
整夜采集守护（watchdog）
================================================================================
职责：定期检查 multi_board_tracker 是否在正常采集，异常时**自动重启**。

为什么不用「自动化任务」来监控：
  自动化本质是一个 AI 会话，每轮都要调模型；被 429 限流时会话直接死（已经踩过）。
  而本脚本是纯 Python + 定时轮询，只读写本地文件，**不依赖模型配额**，可靠性高得多。

判断依据（多重交叉，避免误判）：
  ① 日志 mtime        —— 采集跑一轮约 16 分钟，正常时日志会被持续追加
  ② 数据库最新采样时刻 —— heat_samples.sample_ts，正常每 <=30 分钟 + 一轮耗时 推进
  ③ 进程存活          —— ctypes 读进程命令行，精确识别是不是 multi_board_tracker

处置策略：
  · 日志/库都新鲜            → 正常，什么都不做
  · 进程不存在               → 重启
  · 进程在但日志与库双双停滞 → 判定卡死：杀掉 + 重启
  · 只有日志停滞、库在推进   → 可能只是缓冲，先告警不动作（避免打断好轮次）

日志：logs/watchdog.log（每条 flush，随时可看）
用法：.venv/Scripts/python.exe -u scripts/watchdog.py
"""
import ctypes
import os
import sqlite3
import subprocess
import sys
import time
from ctypes import wintypes
from datetime import datetime, timedelta, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PY = sys.executable
TRACKER = os.path.join(ROOT, 'scripts', 'multi_board_tracker.py')
DB = os.path.join(ROOT, 'data', 'multi_boards.db')
RUNLOG = os.path.join(ROOT, 'logs', 'night_run.log')
WATCHLOG = os.path.join(ROOT, 'logs', 'watchdog.log')
CST = timezone(timedelta(hours=8))

CHECK_INTERVAL = 300          # 每 5 分钟检查一次
STALE_LIMIT_MIN = 45          # 日志/库超过这么久没动静就视为异常
# 守护结束时刻：采集的「最后一轮」在 09:00 之后才开跑（约 16 分钟），
# 所以守护要晚于它退出，否则最后一轮没人看护。
END_HHMM = (9, 40)
TRACKER_ARGS = [
    '--until', '09:00', '--interval', '30', '--per-board', '50',
    '--max-post-per-round', '30', '--max-comment-pages', '5',
    '--max-req-per-round', '400',
]

_fh = None


def log(msg):
    global _fh
    if _fh is None:
        os.makedirs(os.path.dirname(WATCHLOG), exist_ok=True)
        _fh = open(WATCHLOG, 'a', encoding='utf-8')
    line = f'[{datetime.now(CST).strftime("%Y-%m-%d %H:%M:%S")}] {msg}'
    print(line)
    _fh.write(line + '\n')
    _fh.flush()


# ------------------------------------------------------------- 进程识别
#
# ⚠️ 2026-09-22 实测：读别的进程**命令行**需要 PROCESS_VM_READ，本机权限不足
# （OpenProcess 失败），所以"靠命令行认出 multi_board_tracker"这条路走不通。
# 改用 **pidfile**：watchdog 自己拉起的采集进程会把 PID 写进 logs/tracker.pid，
# 之后用 OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION) + GetExitCodeProcess
# 判断该 PID 是否还活着（这两项普通权限即可）。
#
# 对外部已启动的采集（没有 pidfile），退化为用**日志/入库新鲜度**判断——
# 这本来就是最可靠的间接指标，pidfile 只是让"精确杀掉旧进程"成为可能。

PIDFILE = os.path.join(ROOT, 'logs', 'tracker.pid')
STILL_ACTIVE = 259


def pid_alive(pid):
    """判断 PID 是否仍存活（不需要管理员权限）。"""
    if not pid:
        return False
    k32 = ctypes.windll.kernel32
    h = k32.OpenProcess(0x00100000 | 0x1000, False, int(pid))  # SYNCHRONIZE|QUERY_LIMITED
    if not h:
        return False
    try:
        code = wintypes.DWORD()
        if not k32.GetExitCodeProcess(h, ctypes.byref(code)):
            return False
        return code.value == STILL_ACTIVE
    finally:
        k32.CloseHandle(h)


def read_pidfile():
    try:
        if os.path.exists(PIDFILE):
            return int(open(PIDFILE, encoding='utf-8').read().strip())
    except Exception:                                           # noqa: BLE001
        pass
    return 0


def write_pidfile(pid):
    try:
        os.makedirs(os.path.dirname(PIDFILE), exist_ok=True)
        with open(PIDFILE, 'w', encoding='utf-8') as f:
            f.write(str(pid))
    except Exception:                                           # noqa: BLE001
        pass


def kill_pidfile_proc():
    """只杀 watchdog 自己记录的那个 PID（绝不误伤其他 python 进程）。"""
    pid = read_pidfile()
    if pid and pid_alive(pid):
        try:
            subprocess.run(['taskkill', '/PID', str(pid), '/F'],
                           capture_output=True, text=True, timeout=20)
            return True
        except Exception:                                       # noqa: BLE001
            return False
    return False


# ------------------------------------------------------------- 健康检查

def mins_since(path):
    if not os.path.exists(path):
        return 10 ** 6
    return (time.time() - os.path.getmtime(path)) / 60.0


def mins_since_db_sample():
    if not os.path.exists(DB):
        return 10 ** 6
    try:
        c = sqlite3.connect(DB, timeout=10)
        v = c.execute('SELECT MAX(sample_ts) FROM heat_samples').fetchone()[0]
        c.close()
        if not v:
            return 10 ** 6
        dt = datetime.strptime(v, '%Y-%m-%d %H:%M:%S').replace(tzinfo=CST)
        return (datetime.now(CST) - dt).total_seconds() / 60.0
    except Exception:                                           # noqa: BLE001
        return 10 ** 6


def db_quick_stats():
    try:
        c = sqlite3.connect(DB, timeout=10)
        r = {
            'topics': c.execute('SELECT COUNT(*) FROM topics').fetchone()[0],
            'active': c.execute('SELECT COUNT(*) FROM topics WHERE is_active=1').fetchone()[0],
            'heat': c.execute('SELECT COUNT(*) FROM heat_samples').fetchone()[0],
            'posts': c.execute('SELECT COUNT(*) FROM topic_posts').fetchone()[0],
            'cmts': c.execute('SELECT COUNT(*) FROM comments').fetchone()[0],
            'rounds': c.execute('SELECT COUNT(*) FROM crawl_log').fetchone()[0],
            'reqs': c.execute('SELECT COUNT(*) FROM req_audit').fetchone()[0],
        }
        c.close()
        return r
    except Exception as e:                                      # noqa: BLE001
        return {'err': str(e)}


def throttle_count():
    """统计风控/异常响应次数。"""
    try:
        c = sqlite3.connect(DB, timeout=10)
        n = c.execute("""SELECT COUNT(*) FROM req_audit
                         WHERE status LIKE 'HTTP4%' OR status='BLOCKED'
                            OR status LIKE '%THROTTLE%'""").fetchone()[0]
        c.close()
        return n
    except Exception:                                           # noqa: BLE001
        return -1


# ------------------------------------------------------------- 启动/重启

def start_tracker():
    """以追加方式把输出写入运行日志，后台启动采集，并记录 PID。"""
    os.makedirs(os.path.dirname(RUNLOG), exist_ok=True)
    fh = open(RUNLOG, 'a', encoding='utf-8')
    fh.write(f'\n\n===== watchdog 于 '
             f'{datetime.now(CST).strftime("%Y-%m-%d %H:%M:%S")} 重启采集 =====\n')
    fh.flush()
    try:
        # 不带 --probe-again：榜单表已写好，避免每次重启都重复探测
        args = [PY, '-u', TRACKER] + TRACKER_ARGS
        proc = subprocess.Popen(args, cwd=ROOT, stdout=fh,
                                stderr=subprocess.STDOUT,
                                creationflags=subprocess.CREATE_NEW_PROCESS_GROUP)
        write_pidfile(proc.pid)
        return True, ''
    except Exception as e:                                      # noqa: BLE001
        return False, str(e)


def kill_pids(pids):
    for pid in pids:
        try:
            subprocess.run(['taskkill', '/PID', str(pid), '/F'],
                           capture_output=True, text=True, timeout=20)
        except Exception:                                       # noqa: BLE001
            pass


# ------------------------------------------------------------- 主循环

def compute_end():
    """守护的结束时刻。

    启动时间已在 09:00 之后（例如今晚 23:00 启动）→ 结束时刻是**次日** 09:00；
    启动时间在 09:00 之前 → 结束时刻是当日 09:00。
    （踩过的坑：最初写成 `now.hour >= 9` 就直接退出，结果 23:00 启动瞬间自杀。）
    """
    now = datetime.now(CST)
    end = now.replace(hour=END_HHMM[0], minute=END_HHMM[1], second=0, microsecond=0)
    if now >= end:
        end += timedelta(days=1)
    return end


def main():
    end_at = compute_end()
    log('=' * 60)
    log('守护启动：每 5 分钟检查一次，异常自动重启')
    log(f'  采集脚本: {TRACKER}')
    log(f'  运行日志: {RUNLOG}')
    log(f'  判定阈值: 日志/库停滞超过 {STALE_LIMIT_MIN} 分钟视为异常')
    log(f'  结束时刻: {end_at.strftime("%Y-%m-%d %H:%M")}')

    n_check = 0
    while True:
        if datetime.now(CST) >= end_at:
            log('到达结束时刻，守护正常退出。')
            break

        n_check += 1
        log_mins = mins_since(RUNLOG)
        db_mins = mins_since_db_sample()
        pid = read_pidfile()
        proc_ok = pid_alive(pid)
        st = db_quick_stats()
        th = throttle_count()

        log(f'#{n_check} pidfile={pid or "无"}("{"存活" if proc_ok else "不可确认"}") | '
            f'日志 {log_mins:.0f} 分钟前 | 入库 {db_mins:.0f} 分钟前 | '
            f'话题{st.get("topics","?")}(在榜{st.get("active","?")}) '
            f'热度{st.get("heat","?")} 正文{st.get("posts","?")} '
            f'评论{st.get("cmts","?")} 轮次{st.get("rounds","?")} '
            f'请求{st.get("reqs","?")} 风控{th}')

        if th > 0:
            log(f'  ⚠️ 出现 {th} 次风控/异常响应 —— 若持续增长需人工介入')

        stale_log = log_mins > STALE_LIMIT_MIN
        stale_db = db_mins > STALE_LIMIT_MIN

        if stale_log and stale_db:
            log(f'  ❗日志与入库双双停滞超过 {STALE_LIMIT_MIN} 分钟 → 判定采集已死/卡死')
            if proc_ok:
                log(f'  · 杀掉 pidfile 记录的进程 {pid}')
                kill_pidfile_proc()
                time.sleep(5)
            ok, err = start_tracker()
            log(f'  · 重启{"成功，新 PID=" + str(read_pidfile()) if ok else "失败 " + err}')
        elif stale_log and not stale_db:
            log('  · 日志暂未更新但仍在入库 —— 可能只是输出缓冲，继续观察（不动作）')
        else:
            log('  ✅ 正常')

        time.sleep(CHECK_INTERVAL)

    if _fh:
        _fh.close()
    return 0


if __name__ == '__main__':
    sys.exit(main())
