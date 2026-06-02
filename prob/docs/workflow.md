# Prob Monitor — 项目工作流文档

## 项目目标

对 Polymarket 中与**原油、黄金、白银、铜**相关的二元市场做分钟级滚动监控、异常检测 → 推送到飞书群。

## 核心架构

```
scheduler.py          # 主调度器，每分钟触发 run_minute_job + 每天美东0点触发 run_discovery + run_classify
├─ run_minute_job.py  # 分钟级数据采集、异常检测、飞书推送
│  ├─ polymarket.py   # 调 API 获取 midpoint 概率 + volume
│  ├─ anomaly.py      # 计算变化率、阈值判断
│  ├─ storage.py      # 读取合约列表、保存指标/告警
│  ├─ database.py     # SQLite 连接/初始化
│  └─ notifier.py     # 构建消息 + 飞书 API 推送
├─ run_discovery.py   # 从 Polymarket 发现新合约
└─ run_classify.py    # 规则+LLM 合约过滤/方向判定

app/server.py         # Flask 可视化 (8510 端口)

processor/            # 数据处理层
├─ database.py        # 数据库初始化、连接管理
├─ storage.py         # 合约/指标/告警 CRUD + 规则过滤 + prune
├─ anomaly.py         # 异常检测算法
├─ notifier.py        # 飞书消息推送（纯 API 直连）
└─ llm_analyzer.py    # DeepSeek LLM 分类（当余额不足，回退规则）

config/               # 配置
├─ settings.py        # 路径、参数、模块配置
├─ time_utils.py      # 美东时区工具
├─ logger.py          # 日志配置
└─ helpers.py         # 环境变量加载
```

## 核心数据流

### 分钟级循环（每 1 分钟）

```
scheduler.py (每分钟)
  └─ run_minute_job.run()
       ├─ init_db()                          # 确保表存在
       ├─ _clean_expired_by_date()           # 清理已到期合约（每天一次）
       ├─ load_contracts()                   # 从 contracts 表加载有效合约
       ├─ ThreadPoolExecutor (8 workers)
       │  └─ fetch_midpoint + fetch_volume   # 调 Polymarket API
       ├─ compute_change()                   # 对比 1h 前的概率
       ├─ 条件: pct_change >= 5% → alert     # 异常检测
       ├─ save_metric_rows()                 # 写 minute_metrics 表
       ├─ save_alert_rows()                  # 写 alerts 表
       ├─ 去重检查: 该合约 1h 内是否推过？    # 读 alerts 表（按 ET 时间）
       ├─ send_feishu_alerts()               # 推送飞书群（若有新增）
       └─ prune_before()                     # 清理 24h 外数据
```

### 每日发现+分类（美东 0:00）

```
scheduler.py (美东0点)
  ├─ run_discovery.run()
  │    └─ 搜索 Polymarket 市场 → 匹配关键词 → 写入 contracts 表
  └─ run_classify.run()
       ├─ 规则过滤: 剔除球队名、奖项名、人名等噪音
       ├─ 规则方向: 关键词匹配 dip-to/below → 看跌
       ├─ (可选) LLM 分类: DeepSeek 分析相关性和方向
       └─ 写入 contract_classification 表
```

## 数据库（SQLite: `data/state/prob_monitor.db`）

| 表名 | 用途 | 关键字段 |
|------|------|---------|
| `contracts` | 有效合约列表 | `contract_slug`, `group_key`, `market_slug`, `yes_token_id` |
| `contract_classification` | 合约分类结果 | `contract_slug`, `group_key`, `relevant`, `direction`, `reject_reason` |
| `minute_metrics` | 分钟级指标 | `ts_et`, `contract_slug`, `probability`, `volume`, 变化率, 异常标记 |
| `alerts` | 异常告警记录 | `ts_et`, `contract_slug`, `group_key`, `old_value`, `new_value`, `change_ratio` |

## 飞书推送

- **App ID**: `cli_aa8027366778dcba`（独立机器人，与旧 bot 无关）
- **群聊**: 中和动力的第一只🦞 (`oc_3c0431f7c0de028de5237bfe5363720c`)
- **协议**: 飞书 Open API（`POST /open-apis/im/v1/messages`）
- **格式**:
  ```
  ⚠️ 异常合约检测

  【黄金】03:26
    合约名 → 72.5%→81.5% (+9.0%) 🟢 📈看涨

  【原油】03:26
    合约名 → 38.5%→32.0% (-6.5%) 🔴 📉看跌

  【白银】03:26
    ...
  ```

### 方向信号

| 合约方向 | 概率↑ | 概率↓ |
|---------|-------|-------|
| 看涨 | 📈看涨 | 📉看跌 |
| 看跌 (HIT LOW/below) | 📉看跌 | 📈看涨 |

### 去重策略

- **同一个合约 1 小时内不重复推送**（基于 ET 时区 `ts_et` 对比）
- 1 小时后若该合约再次触发异常，重新推送

## 运行命令

```bash
# 启动调度器（每分钟采集 + 异常推送）
.venv\Scripts\python crawler\scheduler.py

# 启动可视化页面 (8510)
.venv\Scripts\python app\server.py

# 手动触发合约发现
.venv\Scripts\python crawler\run_discovery.py

# 手动触发合约分类
.venv\Scripts\python crawler\run_classify.py
```

## 进程管理

- **scheduler**: PID 记录在进程列表（`python.exe crawler/scheduler.py`）
- **server**: PID 记录在进程列表（`python.exe app/server.py`）
- **清理旧进程**: `taskkill /F /PID <PID>`
- **启动方式**: `subprocess.Popen` + `CREATE_NO_WINDOW`

## 约束

- LLM (DeepSeek) 当前余额不足，`contract_classification.direction` 多数存为"中性"
- `notifier.py` 中 `_resolve_signal` 有 slug 关键词启发式判断兜底
- 日志在 `logs/` 目录，分钟任务日志: `crawler_minute_task.log`
