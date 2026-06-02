# Prob Monitor - Polymarket 实时监控

对 Polymarket 中金/银/原油/铜相关二元市场做分钟级滚动监控，保留 24h 滚动窗口，异常检测推送到飞书群。

## 目录结构

```
prob/
├── crawler/          # 发现合约、分钟级采集、调度
│   ├── scheduler.py    # 主调度器（每分钟跑 + 每天0点发现&分类）
│   ├── run_minute_job.py  # 分钟级数据采集+异常检测+飞书推送
│   ├── run_discovery.py   # 从 Polymarket 发现新合约
│   ├── run_classify.py    # 规则+LLM 合约分类（相关/方向）
│   ├── backfill.py        # 历史数据回填
│   ├── polymarket.py      # Polymarket API 封装
│   └── http_client.py     # HTTP 客户端工具
├── processor/        # 数据处理层
│   ├── database.py      # SQLite 初始化、读写
│   ├── storage.py       # 指标/告警/合约存储+规则过滤
│   ├── anomaly.py       # 异常检测算法
│   ├── notifier.py      # 飞书消息推送（纯 API 直连）
│   └── llm_analyzer.py  # LLM 分类分析（DeepSeek）
├── app/              # 可视化服务
│   ├── server.py        # Flask 服务 (8510端口)
│   └── standalone.html  # 前端单页
├── config/           # 配置模块
│   ├── settings.py      # 路径/参数/LLM配置
│   ├── logger.py        # 日志配置
│   ├── time_utils.py    # 时区工具 (ET)
│   └── helpers.py       # 通用工具函数
├── data/
│   └── polymarket.db    # SQLite 数据库
├── logs/             # 日志目录
├── .env              # 环境变量
└── requirements.txt  # Python 依赖
```

## 运行方式

```bash
cd D:\ivan\prob
.venv\Scripts\activate

# 启动调度器（每分钟采集 + 异常推送）
python crawler\scheduler.py

# 启动可视化页面
python app\server.py

# 手动触发合约分类
python crawler\run_classify.py
```

浏览器访问：`http://127.0.0.1:8510`

## 功能概述

- **分钟级采集**：每 1 分钟获取所有合约的 midpoint 概率和 volume
- **异常检测**：基于 1h 对比，概率变化 ≥5% 标记为异常
- **飞书推送**：自动推送到飞书群，24h 内同一合约不重复推送
- **合约分类**：每日 0 点自动发现+分类，规则+LLM 双重过滤
- **页面展示**：分模块（金/银/原油/铜）展示合约卡片和图表
