# 销售数据爬取与日报

本项目使用 Python 定时抓取 `https://web.azpdl.cn/sale/info` 的“收银销售数据”，每日生成分区化数据集（CSV）、Markdown 报告与 SVG 图表，并通过 GitHub Actions 自动运行与提交。

## 功能概览
- 每日 12:10（北京时间，UTC 04:10）自动抓取并落盘
- 抓取范围：
  - 集团合计：昨日、本月累计、本年累计（单位：万元）
  - 各业态：昨日、本月累计、本年累计明细
  - 各门店：昨日、本月累计、本年累计明细（含 `record_id`、`store_code`）
- 输出：
  - 分区目录：`data/YYYY/MM/DD/`
  - CSV：`sales.csv`（长表）、`sales_group.csv`、`sales_business_type.csv`、`sales_store.csv`
  - 报告：`report.md`（含 Markdown 表格与 Top10 SVG 柱状图）

## 数据结构
- 长表字段：
  - `report_date`、`period(daily|monthly|yearly)`、`entity_type(group|business_type|store)`、`entity_name`
  - `sales_wan`、`occurred_at_ms`、`store_code`、`record_id`、`fetched_at_shanghai`
- 拆分表字段：
  - group：`report_date, period, sales_wan, occurred_at_ms, fetched_at_shanghai`
  - business_type：`report_date, period, business_type, sales_wan, occurred_at_ms, fetched_at_shanghai`
  - store：`report_date, period, store_name, store_code, record_id, sales_wan, occurred_at_ms, fetched_at_shanghai`

## 本地运行
```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python scripts/scrape_azpdl_sales.py
```
生成结果位于 `data/YYYY/MM/DD/`。

## GitHub Actions
- 工作流：`.github/workflows/scrape.yml`
- 定时：每天 UTC 04:10（北京时间 12:10）
- 自动提交：对 `data/**` 的新增与变更进行提交

## 注意
- 页面数据每日 12 点更新“昨日及累计销售”，故调度设置为 12:10（北京时间）。
- 若后续页面拆分出公开 API，可直接请求 JSON 并保留向后兼容。
- 项目不引入前端依赖，SVG 由脚本直接生成，仓库可直接预览。
