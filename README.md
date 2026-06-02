# Option IV Monitor — 期权隐含波动率监控系统

从 **预测市场 → 期权隐含波动率爬虫 → 精确隐波计算** 的三层监控体系。

```
┌─────────────────────────────────────────────────┐
│  Layer 1: prob/                                 │
│  预测市场信号发现（Polymarket）                   │
│  宏观事件 → 概率 → 市场情绪探测                   │
├─────────────────────────────────────────────────┤
│  Layer 2: vol/                                  │
│  期权隐波爬虫（OpenVlab 页面数据）                 │
│  全品种 IV 扫描 + 实时告警推送                     │
├─────────────────────────────────────────────────┤
│  Layer 3: RQvol/                                │
│  期权隐波精算（RQData 专业数据源）                 │
│  分钟级 Greeks → IV 变化检测 + 自动预警            │
└─────────────────────────────────────────────────┘
```

---

## 📊 Layer 1 — 预测市场信号 (prob/)

抓取 [Polymarket](https://polymarket.com) 预测市场数据，将**事件概率**映射为隐含概率，宏观事件对市场的潜在影响。

### 核心功能

| 模块 | 文件 | 职责 |
|------|------|------|
| 爬虫 | `crawler/polymarket.py` | Polymarket API 数据抓取 |
| 调度器 | `crawler/scheduler.py` | 定时任务编排 |
| 分析器 | `processor/llm_analyzer.py` | LLM 事件分析 |
| 宏观点预测 | `processor/macro_forecast.py` | 宏观事件概率 → 市场影响 |
| 报告生成 | `processor/hourly_report.py` | 定时报告生成 |
| 异常检测 | `processor/anomaly.py` | 预测市场异常波动检测 |
| Web 服务 | `app/server.py` | 报告展示页面 |

### 工作流

```
Polymarket API
    ↓
爬虫获取事件和概率 (scheduler / run_minute_job)
    ↓
LLM 分析事件影响 (llm_analyzer)
    ↓
宏观预测 / 异常检测 (macro_forecast / anomaly)
    ↓
通知推送 + 报告 (notifier / hourly_report)
```

### 配置

编辑 `config/settings.py`：
- LLM API Key (`llm_api_key.txt` 中写入)
- 推送渠道（企业微信 / Discord 等）
- 调度频率

### 启动

```bash
python start_services.py    # 全服务启动
python watchdog.py          # 看门狗（崩溃自恢复）
```

---

## 🕷️ Layer 2 — 隐波爬虫 (vol/)

基于 [OpenVlab](https://openvlab.com) 的期权隐波数据爬虫，覆盖 **国内全部期权品种**，每 60 秒轮询监测隐波变化。

### 监控品种

**ETF 期权：**
| 标的 | 代码 | 交易所 |
|------|------|--------|
| 上证 50ETF | 510050.XSHG | 上交所 |
| 沪深 300ETF | 510300.XSHG | 深交所 |
| 中证 500ETF | 510500.XSHG | 上交所 |
| 科创 50ETF | 588000.XSHG | 上交所 |

**商品期货期权：**
| 品种 | 代码 |
|------|------|
| 沪铜 | CU |
| 沪金 | AU |
| 螺纹钢 | RB |
| 原油 | SC |
| 中证1000期货 | MO |

### 核心文件

| 文件 | 职责 |
|------|------|
| `crawler.py` | OpenVlab 页面数据抓取引擎 |
| `iv_guard.py` | **隐波变化预警守护进程**（每分钟轮询 + 企业微信推送） |
| `iv_api_scanner.py` | API 模式隐波扫描 |
| `probe_api.py` | OpenVlab API 探测 |
| `market_utils.py` | 市场工具函数 |
| `iv_watchdog.py` | 看门狗（守护 iv_guard 持续运行） |

### 预警逻辑

| 变化幅度 | 图标 | 标题 |
|---------|------|------|
| IV 上升 ≥ 5% | 🚨 | 隐波大幅升高预警 |
| IV 上升 ≥ 3% | 🌶️ | 隐波升高预警 |
| IV 上升 ≥ 2% | ⚠️ | 隐波升高预警 |
| IV 下降 ≥ 3% | 🍀 | 隐波降低预警 |

**推送格式：**
```
[06-02 21:05] ⚠️ 隐波升高预警
上证50ETF-2607-P-2850 隐波变化+2.1%↑
```

### 启动

```bash
python iv_guard.py       # 主监控循环
python iv_watchdog.py    # 看门狗（可选）
```

---

## 📐 Layer 3 — 隐波精算 (RQvol/)

基于 [RQData](https://www.ricequant.com) 专业行情数据源，精确计算期权隐含波动率，分钟级监测 IV 变化。

### 与 Layer 2 的区别

| 维度 | Layer 2 (vol/) | Layer 3 (RQvol/) |
|------|---------------|-----------------|
| 数据源 | OpenVlab 网页 | RQData 量化数据 |
| 精度 | 日频，页面刷新 | 分钟级 Greeks |
| 合约筛选 | 全部品种粗扫 | 近月平值精准筛选 |
| 数据深度 | 仅 IV | IV + Delta/Gamma/Vega/Theta/Rho |
| 推出时机 | 刚上市即覆盖 | 仅纳入有 Greeks 数据的活跃合约 |

### 监控品种

| 标的 | 近月合约区间 |
|------|-------------|
| 上证 50ETF 2607 (7月) | 行权价 2.75~3.30 |
| 沪深 300ETF 2607 (7月) | 行权价 3.80~5.50 |
| 中证 500ETF 2607 (7月) | 行权价 5.00~8.00 |
| 科创 50ETF 2607 (7月) | 行权价 1.45~2.00 |

### 核心文件

| 文件 | 职责 |
|------|------|
| `rq_option_scanner.py` | 主监控脚本（轮询 + 推送） |

### 交易时段

| 时段 | 时间 |
|------|------|
| 上午 | 09:30 ~ 11:30 |
| 下午 | 13:00 ~ 15:00 |
| 夜盘 | 21:00 ~ 次日 02:30 |

非交易时段自动静默，`alerted_set` 清空重新计数。

### 启动

```bash
cd RQvol
python rq_option_scanner.py
```

### 配置

编辑 `rq_option_scanner.py` 顶部常量：

```python
LICENSE_KEY = "YOUR_RQDATAC_LICENSE_KEY"   # RQData 许可证
WECOM_KEY = "YOUR_WECOM_WEBHOOK_KEY"       # 企业微信机器人 Webhook
```

---

## 🚀 完整部署推荐

### 最低配置
- Python 3.10+
- Windows / Linux / macOS

### 依赖安装

```bash
# RQData
pip install rqdatac pandas requests

# OpenVlab 爬虫
pip install requests beautifulsoup4 lxml

# Polymarket
pip install aiohttp websockets jinja2
```

### 推荐拓扑

```
┌────────────────────────────────────────────┐
│ NOC (network operations center)                │
│                                              │
│  09:25  prob/ → 宏观事件扫描 + LLM分析       │
│  09:30  vol/ + RQvol/ → 开盘隐波基线扫描      │
│  21:00  vol/ + RQvol/ → 夜盘隐波监控          │
│                                              │
│  实时推送企业微信 🔔                           │
└────────────────────────────────────────────┘
```

---

## 🔐 note

本项目代码已脱敏处理，实际运行需要：
1. **API 许可
2. **企业微信机器人 Webhook** — 在群设置中添加机器人获取
3. **LLM API Key** — OpenAI / Claude 等

请将密钥写入环境变量或独立配置文件。


