# 微博热搜生命周期追踪爬虫

> 追踪微博热搜话题从「上榜 → 在榜 → 下榜 → 后续热度衰减」的完整生命周期，
> 输出一份结构化的事件数据集（事件信息 + 热度轨迹 + 评论 + 配图）。

一个用 Python 编写的微博数据采集工具。它不做"广撒网式"的批量抓取，而是**纵向跟踪单条热搜**：
记录话题什么时候上的热搜、在榜期间热度如何变化、什么时候下的热搜，并抓取该话题下最热微博的
正文与评论，最终对齐到一张 24 列的事件表。

---

## 目录

- [项目背景](#项目背景)
- [核心特性](#核心特性)
- [实测效果](#实测效果)
- [技术栈](#技术栈)
- [核心链路](#核心链路)
- [目录结构](#目录结构)
- [快速开始](#快速开始)
- [使用说明](#使用说明)
- [数据表结构](#数据表结构)
- [数据质量与口径](#数据质量与口径)
- [常见问题](#常见问题)
- [注意事项](#注意事项)

---

## 项目背景

面向"突发事件数据采集"类课程作业。要求：

- 采集**当天发生**、**上过热榜**的突发事件
- 记录事件内容、来源、发生时间、上下热榜时间、类型
- 跟踪上热搜后 **1/2/4/6/…/96 小时**的热度变化（"阅读量"轨迹）
- 抓取事件的**评论**（内容 + 点赞数）与**配图**

本仓库是其中的**微博**部分实现。

---

## 核心特性

| 特性 | 说明 |
|---|---|
| **反爬绕过** | 用 `curl_cffi` 伪装 Chrome 的 TLS 指纹，这是能请求通微博接口的前提 |
| **诚实的三种时点状态** | `recorded`（采到值）/ `offboard`（已下榜）/ `missed`（漏采）—— 后两者一律**留空，绝不伪造** |
| **断点续采** | 分段落库（快照 → 热度 → 每条正文+评论 → 轮次日志各 commit 一次），进程被中断最多丢"正在抓的这一条" |
| **风控保护** | 请求限速、连续搜索熔断、403/432 立即停止、中立站点对照（区分"被封"与"本地网络故障"） |
| **时间倒挂修复** | 只选取**上热搜之前**发布的微博作为事件正文，保证"事件发生时间 ≤ 上热搜时间" |
| **一键导出** | 直接产出与模板逐列一致的 `事件.xlsx` / `事件评论信息.xlsx` / 配图目录 / zip |
| **数据分析报告** | `gen_report.py` 自动生成统计报告 + 5 张图表 |

---

## 实测效果

单次连续采集（约 8 小时 / 14 轮采样，全程 **0 次风控**）的累计数据：

| 指标 | 数值 |
|---|---|
| 追踪话题 | 163 |
| 有完整正文的事件 | 109 |
| 评论 | 6852（含二级回复 1021） |
| 热度轨迹点 | 914 |
| 事件配图 | 126 张（覆盖 48 个事件） |
| 热搜在榜时长 | 均值 63 分钟，**中位数 28 分钟** |

> 仓库内的 `sample_data/` 仅含少量脱敏示例，完整数据集不公开。

---

## 技术栈

- **Python 3.10+**
- **curl_cffi** —— 伪装浏览器 TLS 指纹（核心）
- **SQLite** —— 本地存储，标准库自带
- **openpyxl** —— 导出 Excel
- **matplotlib** ——（可选）生成统计图表
- **websocket-client** ——（可选）通过 CDP 从浏览器读取登录 Cookie

---

## 核心链路

```
热搜榜(1 次请求) ──► 追踪库：上热搜时间 / 下热搜时间 / 每小时热度快照
                          │
                          └─► 详情(每条约 5~8 次请求) ──► 最热微博的
                                                        正文 / 账号 / 发布时间 / 配图
                                                        + 该微博的一级评论与二级回复
                          │
导出 ──► 事件.xlsx(24 列) + 事件评论信息.xlsx(4 列) + 事件图/P{n}/
```

**关键概念：「正文」不是额外字段，而是评论与配图的前置。**
热搜榜只给一个词和一个热度数字；要填「事件内容 / 来源 / 事件发生时间 / 图片 / 评论信息」，
必须先抓到那条具体微博（**这就是"正文"**）。评论接口 `comments/hotflow?id={mid}`
是挂在这条微博上的——没有它的 id，评论一条也拿不到。

---

## 目录结构

```
weibo-hotsearch-tracker/
├── README.md
├── requirements.txt
├── .gitignore
├── agent-browser.example.json     # 浏览器登录配置示例
├── config/
│   ├── cookie.example.txt         # Cookie 配置模板（真实 Cookie 不要提交）
│   ├── proxies.example.txt        # 代理配置模板
│   └── domain_keywords.txt        # 领域补充词表（可选）
├── scripts/
│   ├── event_crawler.py           # 采集核心库（HTTP 封装 / 搜索 / 正文 / 评论 / 安全护栏）
│   ├── hot_tracker.py             # ★ 生命周期追踪器主体（--tick / --status / --loop）
│   ├── export_tracker.py          # ★ 导出为模板格式（xlsx + 配图 + zip）
│   ├── gen_report.py              # 生成统计报告 + 图表
│   ├── login_then_crawl.py        # 一条龙：开浏览器 → 登录 → 导 Cookie → 采集
│   ├── decrypt_profile_cookies.py # 从浏览器 profile 解密 Cookie
│   ├── probe_weibo.py             # 连通性体检（只发 2 次请求）
│   └── safety.py                  # 安全护栏（限速 / 熔断 / 审计）
├── dataset_template/              # 目标数据模板（24 列格式参考）
└── sample_data/                   # 脱敏示例数据
```

---

## 快速开始

### 1. 安装依赖

```bash
git clone https://github.com/xingchenyd/weibo-hotsearch-tracker.git
cd weibo-hotsearch-tracker

python -m venv .venv
# Windows:
.venv\Scripts\activate
# macOS / Linux:
source .venv/bin/activate

pip install -r requirements.txt
```

### 2. 配置 Cookie

大部分接口（榜单、正文、评论）**匿名即可访问**；但**关键词搜索**是登录墙，必须带 Cookie。

**方式一（推荐，不落盘）：**

```bash
# Windows PowerShell
$env:WEIBO_COOKIE="SUB=你的值; SUBP=你的值; SSOLoginState=你的值"

# Git Bash / macOS / Linux
export WEIBO_COOKIE="SUB=你的值; SUBP=你的值; SSOLoginState=你的值"
```

**方式二（写入文件，注意不要提交）：**

```bash
cp config/cookie.example.txt config/cookie.txt
# 编辑 config/cookie.txt，填入真实 Cookie
```

**怎么取 Cookie：** 浏览器登录 `weibo.com` → F12 开发者工具 → Network → 刷新页面 →
点任意请求 → Headers → 复制 `Cookie:` 后面整行。

### 3. 体检连通性（强烈建议先跑）

```bash
python scripts/probe_weibo.py
```

只发 **2 次**请求，分接口告诉你哪些通、哪些不通。**注意：如果你开了 VPN / 代理，
微博域名的 TLS 握手可能被劫持导致全部失败——这不是封号，关掉代理或给微博域名设直连即可。**

### 4. 运行一次采集

```bash
python scripts/hot_tracker.py --tick --max-detail 12 --comment-pages 3 \
    --max-lag-minutes 35 --offboard-misses 2
```

### 5. 导出数据集

```bash
# 导出（只导有正文的事件，打包 zip）
python scripts/export_tracker.py

# 附带下载事件配图（每个事件最多 5 张）
python scripts/export_tracker.py --fetch-pics --max-pics 5

# 导出全部事件（含还没抓正文的）
python scripts/export_tracker.py --all-events
```

产物在 `dataset_tracker/事件数据集/` 与 `dataset_tracker/事件数据集.zip`。

### 6. （可选）生成分析报告

```bash
python scripts/gen_report.py     # 输出到 reports/：1 份 md 报告 + 5 张图表
```

---

## 使用说明

### `hot_tracker.py` —— 生命周期追踪器（主力）

```bash
# 跑一次（适合定时任务）
python scripts/hot_tracker.py --tick

# 查看当前追踪状态
python scripts/hot_tracker.py --status

# 循环采样模式：在单进程内持续运行，每 N 分钟采一次榜（提高时点精度）
python scripts/hot_tracker.py --tick --loop-minutes 480 --loop-interval 30 --detail-every 2
```

主要参数：

| 参数 | 默认 | 说明 |
|---|---|---|
| `--max-detail N` | 3 | 每轮最多为几条新话题抓正文+评论（**控制请求量的关键**） |
| `--comment-pages N` | 3 | 每条微博抓几页评论（一级 + 二级回复都收） |
| `--max-lag-minutes N` | 35 | 热度时点与最近样本允许的最大偏差；超出即标 `missed` 留空 |
| `--offboard-misses N` | 2 | 连续 N 次不在榜才判定"下热搜"（防榜单抖动） |
| `--loop-minutes N` | 0 | 循环模式运行时长（0 = 只跑一次） |
| `--loop-interval N` | 5 | 循环模式下的采样间隔（分钟） |
| `--detail-every K` | 自动 | 循环模式下每 K 轮抓一次正文 |
| `--min-gap-min N` | 30 | 距上次采样不足 N 分钟则整轮跳过（防重复请求） |

> ⚠️ **不要用 `--align-hour`** 配合短生命周期的自动化会话：它会让进程 `sleep` 到下一个整点
> （最长 60 分钟），可能被宿主回收导致整轮失败。对齐整点应改调度时机，而非进程内长休眠。

### `event_crawler.py` —— 底层采集库

既被 `hot_tracker.py` 引用，也可独立使用（偏"广撒网"的事件发现模式）。

```bash
python scripts/event_crawler.py --mode hot-events --interleave \
    --max-age-hours 24 --max-comment-pages 2
```

### `login_then_crawl.py` —— 一条龙（新手友好）

```bash
python scripts/login_then_crawl.py
```

会弹出浏览器窗口，**你只需手动登录**；之后脚本自动导 Cookie、校验 uid、体检接口、开始采集。
可通过环境变量 `WEIBO_OLD_UID` 指定"上一个账号的 uid"，用于检测是否复用了旧登录态。

---

## 数据表结构

### 事件表（`事件.xlsx`，24 列，与模板逐列一致）

| 列 | 来源 / 说明 |
|---|---|
| 序号 | 自增 |
| 事件名 | 热搜话题词 |
| 事件内容 | 该话题下最热微博的正文 |
| 来源 | 微博账号昵称 |
| 事件发生时间 | 微博发布时间（**只取上热搜之前发布的**） |
| 上头条热榜时间 | 接口 `onboard_time`（精确到秒） |
| 下头条热榜时间 | 最后一次观测到在榜的时刻 |
| 事件结束时间 | 同下热搜时间 |
| 评论信息 | 评论序号标识 `R{n}` |
| 图片 | 配图目录标识 `P{n}` |
| 类型 | 微博官方分类 |
| 是否为虚假 | 留空（需人工判断） |
| 1小时 ~ 96小时 | 上热搜后各时点的**热搜热度值** |

### 评论表（`事件评论信息.xlsx`，4 列）

`ID` / `评论者` / `内容` / `点赞数`

### 配图目录

```
事件图/P{n}/{n}-{k}.jpg     # P{n} 是容器，{n}-{k} 是其中第 k 张
```

---

## 数据质量与口径

### 三种热度时点状态（只有 `recorded` 会写值）

| 状态 | 含义 | 处理 |
|---|---|---|
| `recorded` | 目标时刻 ±35 分钟内采到样本 | 写入真实热度值 |
| `offboard` | 目标时刻前话题已下榜 | **留空**（本来就没有值） |
| `missed` | 话题仍在榜但漏采 | **留空**（绝不用别的时刻冒充） |

### 关于「阅读量」

微博**不对外提供**话题阅读量接口（试过话题页、搜索页、热门话题容器等 10 个端点均无）。
本工具使用**热搜榜热度值 `num`** 作为口径——其数量级为数十万~百万，与常见模板示例
"阅读量 102 万"完全吻合。

### 为什么 24h / 48h / 96h 常常为空？

因为**热搜话题在榜时间极短**（实测中位数仅 **28 分钟**），绝大多数话题活不到那么久，
到点时已下榜（标 `offboard` 留空）。这是数据源特性，不是采集失败。

---

## 常见问题

**Q：报 `SSL_ERROR_SYSCALL` / `Connection closed abruptly`，所有微博域名都连不上？**
A：大概率是 **VPN / 代理劫持了微博的 TLS 握手**，不是封号。诊断方法：探测一个中立网站
（如 `example.com`）——若它正常、只有微博失败，即为代理问题。解决：关闭代理，或在代理软件里
把 `weibo.com` / `weibo.cn` / `sina.com.cn` 设为**直连（DIRECT）**。

**Q：搜索接口一直返回 HTTP 432 / 403？**
A：432 通常是"需要登录"，请配置 Cookie；403 往往是**连续搜索次数过多**触发风控。
本工具已内置"交替模式 + 连续搜索熔断"，请勿关闭，也不要盲目提高并发 / 缩短间隔。

**Q：热度值为空是抓取失败吗？**
A：不一定。请区分三态：`offboard`（话题已下榜）和 `missed`（漏采）都是**有意留空**。
只有在你确认话题当时在榜、且样本偏差超限时才可能是漏采。

**Q：请求额度怎么算？**
A：审计库 `safety_audit.db` 中每条请求写 2 行（`request` + `HTTP200`），
统计请求数**只看 `status='request'`**，否则会翻倍。建议单日控制在 2000 次以内。

---

## 注意事项

1. **本工具仅供学习与技术研究使用**，请遵守目标网站的 `robots.txt` 与服务条款。
2. **切勿提交真实 Cookie / 代理账号密码**到任何公开仓库；本仓库的 `.gitignore` 已排除相关文件。
3. 建议使用**不常用的小号**进行采集，避免影响主账号。
4. 采集请**控制频率**、设置熔断保护。触发风控后应立即停止，而非重试。
5. 未经授权，**不得将采集到的用户评论等数据用于商业用途或公开发布**。

---

## License

仅供学习交流使用。数据版权归原网站及内容作者所有。
