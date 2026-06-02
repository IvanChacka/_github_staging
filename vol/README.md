# vol

这是一个针对 OpenVlab `https://www.openvlab.cn/market` 的隐波监控小项目，核心目标是：

- 定时抓取页面里的 `隐波最大上升` / `隐波最大下降`
- 按阈值判断是否触发预警
- 通过企业微信机器人推送消息
- 用 watchdog 监管主进程，异常时自动重启

## 文件说明

- `iv_guard.py`
  主监控进程。每 60 秒抓一次页面，解析榜单，判断阈值，发送企业微信消息，并持续写心跳文件。

- `iv_watchdog.py`
  监管进程。每 30 秒检查一次 `iv_guard.py` 的心跳；如果主进程退出、卡死或心跳超时，就自动重启。

- `market_utils.py`
  公共抓取与解析模块。负责：
  - 配置 UTF-8 输出
  - 用 Playwright 打开 OpenVlab 页面
  - 直接从页面卡片 DOM 读取 `名称 / 涨幅% / 隐波变化`
  - 在 DOM 抓取失败时，用文本解析作为兜底

- `crawler.py`
  单次抓取脚本。用于手动验证当前抓取结果，并把结果写到 `vol_data.json`。

- `start_iv.vbs`
  推荐入口。启动 `iv_watchdog.py`，由 watchdog 再拉起 `iv_guard.py`。

- `run_guard.bat`
  老的循环启动方式。它会直接无限重启 `iv_guard.py`。
  现在正式运行时不建议再和 `iv_watchdog.py` 同时使用，否则容易出现多开。

- `requirements.txt`
  Python 依赖。

## 运行链路

1. 启动 `start_iv.vbs`
2. `start_iv.vbs` 拉起 `iv_watchdog.py`
3. `iv_watchdog.py` 启动 `iv_guard.py`
4. `iv_guard.py` 每分钟执行一次：
   - 打开 OpenVlab market 页面
   - 抓取 `隐波最大上升` 和 `隐波最大下降` 两张卡片
   - 解析出前 5 个品种
   - 判断是否在交易时段
   - 判断隐波变化是否超过阈值
   - 发送企业微信消息
   - 更新心跳文件
5. `iv_watchdog.py` 持续看心跳：
   - 正常则继续运行
   - 超时 / 异常则杀掉旧进程并重启

## 抓取逻辑

当前抓取不再依赖整页 `body` 文本顺序，而是直接从页面可见卡片 DOM 读取。

主要规则：

- 卡片标题包含 `隐波最大上升` 或 `隐波最大下降`
- 每一行从 `span.truncate` 读取中文名
- 同行第 2 列读取涨幅%
- 同行第 3 列读取隐波变化
- 连续两轮结果一致后再采用，避免页面初始刷新时抓到不稳定数据

## 预警逻辑

配置在 `iv_guard.py`：

- 上升阈值：`THRESHOLD_UP = 2.0`
- 下降阈值：`THRESHOLD_DOWN = 3.0`
- 轮询间隔：`POLL_INTERVAL = 60`

交易时段内才会发预警；休市时会清空已提醒集合。

## 运行产物

运行过程中会生成：

- `iv_guard.heartbeat.json`
  主进程心跳，供 watchdog 判断是否存活

- `vol_data.json`
  手动运行 `crawler.py` 时保存的最新抓取结果

- `__pycache__/`
  Python 缓存目录

## 建议用法

推荐正式运行方式：

```text
start_iv.vbs
```

手动验证抓取：

```text
python crawler.py
```

不推荐同时使用：

- `start_iv.vbs`
- `run_guard.bat`

因为两者都会拉起主进程，可能导致 `iv_guard.py` 多开。
