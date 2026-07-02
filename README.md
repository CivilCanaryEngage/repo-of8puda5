# A股板块主力行为计算工具

每天收盘后自动拉取东方财富板块资金流数据，计算各板块的主力行为并生成报告。

## 公式

- 主力暗盘 = 主力资金 - 散户资金
- 主力强度 = 主力暗盘 / 成交额 × 100
- 主力行为（按主力强度划分）：

| 主力强度 | 主力行为 |
| --- | --- |
| ≥ 3 | 抢筹 |
| [1, 3) | 建仓 |
| (-1, 1) | 洗盘 |
| ≤ -1 | 出货 |

## 数据说明

数据来源为东方财富公开的板块资金流接口（无需 key，仅用标准库，无第三方依赖）：

- 主力资金 = 超大单 + 大单净流入
- 散户资金 = 中单 + 小单净流入
- 成交额、涨幅取当日板块实时/收盘数据

## 使用

```bash
# 行业板块（默认），输出全部并生成 CSV/HTML 报告到 output/
python3 main_force.py

# 概念板块，终端只显示前 30 条
python3 main_force.py --kind concept --top 30

# 地域板块
python3 main_force.py --kind region
```

输出字段：板块、涨幅、成交额、主力资金、散户资金、主力暗盘、主力强度、主力行为（按主力强度降序）。
报告保存为 `output/日期_类型.csv` 与 `output/日期_类型.html`。

## 桌面客户端 (Windows)

`gui.py` 提供图形界面（Tkinter，标准库自带）：板块类型切换、行为筛选、点击表头排序、手动刷新、每日 15:30 自动刷新、导出 CSV。

```bash
python3 gui.py
```

Windows exe 由 GitHub Actions 自动打包（PyInstaller）：在仓库 Actions 页面的 "Build Windows Client" 工作流中下载 `A股主力行为-windows` 构件，解压后双击 exe 即可运行，无需安装 Python。

## 每日收盘后自动运行

A股收盘为北京时间 15:00，建议 15:30 运行。crontab 示例（服务器为北京时间）：

```cron
30 15 * * 1-5 cd /path/to/astock-main-force && python3 main_force.py >> run.log 2>&1
```

若服务器为 UTC 时间（北京时间 = UTC+8，15:30 CST = 07:30 UTC）：

```cron
30 7 * * 1-5 cd /path/to/astock-main-force && python3 main_force.py >> run.log 2>&1
```
