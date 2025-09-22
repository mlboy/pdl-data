#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import csv
import os
import re
import json
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Iterable, List, Tuple

import pytz
import requests
from bs4 import BeautifulSoup

SOURCE_URL = "https://web.azpdl.cn/sale/info"
OUTPUT_DIR = Path("data")

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/126.0.0.0 Safari/537.36"
    )
}


# --------------------------- parsing helpers ---------------------------

def normalize_number(text: str) -> float:
    if text is None:
        raise ValueError("Empty number text")
    cleaned = text.strip().replace(",", "")
    match = re.match(r"^-?\d+(?:\.\d+)?$", cleaned)
    if not match:
        raise ValueError(f"Not a numeric value: {text!r}")
    return float(cleaned)


def first_number_in_text(text: str) -> str | None:
    m = re.search(r"-?\d{1,3}(?:,\d{3})*(?:\.\d+)?|-?\d+(?:\.\d+)?", text)
    return m.group(0) if m else None


def extract_with_label(full_text_lines: List[str], pattern: re.Pattern) -> tuple[float, int]:
    for idx, line in enumerate(full_text_lines):
        if pattern.search(line):
            for j in range(idx + 1, min(idx + 15, len(full_text_lines))):
                candidate = full_text_lines[j].strip()
                if re.search(r"\d", candidate):
                    num_match = first_number_in_text(candidate)
                    if num_match:
                        return normalize_number(num_match), j
    raise ValueError(f"Label not found or number missing for pattern: {pattern.pattern}")


def parse_report_date_from_label(full_text: str) -> datetime:
    tz_cn = pytz.timezone("Asia/Shanghai")
    m = re.search(r"集团合计销售【(\d{4})年(\d{2})月(\d{2})日】", full_text)
    if m:
        y, mo, d = map(int, m.groups())
        return tz_cn.localize(datetime(y, mo, d))
    now_cn = datetime.now(tz_cn)
    return (now_cn - timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)


@dataclass
class SectionSpec:
    key: str
    pattern: re.Pattern
    entity_type: str  # 'business_type' or 'store'
    period: str       # 'daily' | 'monthly' | 'yearly'


def build_section_specs() -> List[SectionSpec]:
    return [
        SectionSpec("bt_daily", re.compile(r"各业态销售【\d{4}年\d{2}月\d{2}日】（?单位：万元）?"), "business_type", "daily"),
        SectionSpec("bt_month", re.compile(r"本月各业态销售（?单位：万元）?"), "business_type", "monthly"),
        SectionSpec("bt_year", re.compile(r"本年各业态销售（?单位：万元）?"), "business_type", "yearly"),
        SectionSpec("store_daily", re.compile(r"各门店销售【\d{4}年\d{2}月\d{2}日】（?单位：万元）?"), "store", "daily"),
        SectionSpec("store_month", re.compile(r"本月各门店销售（?单位：万元）?"), "store", "monthly"),
        SectionSpec("store_year", re.compile(r"本年各门店销售（?单位：万元）?"), "store", "yearly"),
    ]


def find_header_indices(lines: List[str], specs: List[SectionSpec]) -> List[Tuple[int, SectionSpec]]:
    found: List[Tuple[int, SectionSpec]] = []
    for i, ln in enumerate(lines):
        for spec in specs:
            if spec.pattern.search(ln):
                found.append((i, spec))
    found.sort(key=lambda x: x[0])
    return found


def slice_section_blocks(lines: List[str], specs: List[SectionSpec]) -> List[Tuple[SectionSpec, List[str]]]:
    header_positions = find_header_indices(lines, specs)
    blocks: List[Tuple[SectionSpec, List[str]]] = []
    for idx, (start_i, spec) in enumerate(header_positions):
        end_i = header_positions[idx + 1][0] if idx + 1 < len(header_positions) else len(lines)
        block = [ln.strip() for ln in lines[start_i + 1 : end_i] if ln.strip()]
        blocks.append((spec, block))
    return blocks


def parse_block_rows(block_lines: List[str]) -> List[Tuple[str, float]]:
    rows: List[Tuple[str, float]] = []
    i = 0
    while i < len(block_lines):
        line = block_lines[i]
        inline_match = re.match(
            r"^(?P<name>[^:：\-\s].*?)\s*(?:[:：\-]\s*)?(?P<num>-?\d{1,3}(?:,\d{3})*(?:\.\d+)?|-?\d+(?:\.\d+)?)\s*(?:万元)?$",
            line,
        )
        if inline_match:
            name = inline_match.group("name").strip()
            num = normalize_number(inline_match.group("num"))
            rows.append((name, num))
            i += 1
            continue
        if i + 1 < len(block_lines):
            next_line = block_lines[i + 1]
            num_text = first_number_in_text(next_line)
            if num_text and not re.search(r"\d", line):
                try:
                    rows.append((line.strip(), normalize_number(num_text)))
                    i += 2
                    continue
                except Exception:
                    pass
        i += 1
    return rows


def extract_inline_data(html: str) -> dict | None:
    m = re.search(r"var\s+data\s*=\s*(\{[\s\S]*?\});", html)
    if not m:
        m = re.search(r"\bdata\s*=\s*(\{[\s\S]*?\});", html)
    if not m:
        return None
    js_obj = m.group(1)
    try:
        return json.loads(js_obj)
    except Exception:
        cleaned = re.sub(r",\s*([}\]])", r"\1", js_obj)
        return json.loads(cleaned)


def dt_midnight_ms_shanghai(dt: datetime) -> int:
    tz_cn = pytz.timezone("Asia/Shanghai")
    d0 = dt.astimezone(tz_cn).replace(hour=0, minute=0, second=0, microsecond=0)
    epoch = datetime(1970, 1, 1, tzinfo=pytz.UTC)
    return int((d0.astimezone(pytz.UTC) - epoch).total_seconds() * 1000)


# --------------------------- scraping ---------------------------

def fetch_page(url: str) -> str:
    resp = requests.get(url, headers=HEADERS, timeout=30)
    resp.raise_for_status()
    return resp.text


def scrape() -> list[dict]:
    html = fetch_page(SOURCE_URL)
    soup = BeautifulSoup(html, "lxml")

    full_text = soup.get_text("\n")
    lines = [ln.strip() for ln in full_text.splitlines() if ln.strip()]

    # Group totals
    p_daily = re.compile(r"集团合计销售【\d{4}年\d{2}月\d{2}日】（?万元）?")
    p_month = re.compile(r"本月集团合计销售（?万元）?")
    p_year = re.compile(r"本年集团合计销售（?万元）?")

    daily_wan, _ = extract_with_label(lines, p_daily)
    month_wan, _ = extract_with_label(lines, p_month)
    year_wan, _ = extract_with_label(lines, p_year)

    report_dt_cn = parse_report_date_from_label(full_text)
    tz_cn = pytz.timezone("Asia/Shanghai")
    fetched_at = datetime.now(tz_cn).isoformat()
    report_midnight_ms = dt_midnight_ms_shanghai(report_dt_cn)

    records: list[dict] = []

    # Add group rows (occurred_at_ms = report date midnight)
    for period, value in (("daily", daily_wan), ("monthly", month_wan), ("yearly", year_wan)):
        records.append({
            "report_date": report_dt_cn.strftime("%Y-%m-%d"),
            "period": period,
            "entity_type": "group",
            "entity_name": "集团合计",
            "sales_wan": value,
            "occurred_at_ms": report_midnight_ms,
            "store_code": "",
            "record_id": "",
            "fetched_at_shanghai": fetched_at,
        })

    # Prefer inline JSON data for detailed sections if available
    inline = extract_inline_data(html)
    if inline:
        # Business types
        bu_list = inline.get("buData") or []
        for item in bu_list:
            name = item.get("业态") or item.get("name") or ""
            occurred_ms = int(item.get("销售发生时间")) if item.get("销售发生时间") else report_midnight_ms
            if name:
                if "销售" in item:
                    records.append({
                        "report_date": report_dt_cn.strftime("%Y-%m-%d"),
                        "period": "daily",
                        "entity_type": "business_type",
                        "entity_name": name,
                        "sales_wan": float(item.get("销售", 0)),
                        "occurred_at_ms": occurred_ms,
                        "store_code": "",
                        "record_id": "",
                        "fetched_at_shanghai": fetched_at,
                    })
                if "月度累计销售金额" in item:
                    records.append({
                        "report_date": report_dt_cn.strftime("%Y-%m-%d"),
                        "period": "monthly",
                        "entity_type": "business_type",
                        "entity_name": name,
                        "sales_wan": float(item.get("月度累计销售金额", 0)),
                        "occurred_at_ms": occurred_ms,
                        "store_code": "",
                        "record_id": "",
                        "fetched_at_shanghai": fetched_at,
                    })
                if "年度累计销售金额" in item:
                    records.append({
                        "report_date": report_dt_cn.strftime("%Y-%m-%d"),
                        "period": "yearly",
                        "entity_type": "business_type",
                        "entity_name": name,
                        "sales_wan": float(item.get("年度累计销售金额", 0)),
                        "occurred_at_ms": occurred_ms,
                        "store_code": "",
                        "record_id": "",
                        "fetched_at_shanghai": fetched_at,
                    })
        # Stores
        shop_list = inline.get("shopData") or []
        for item in shop_list:
            name = item.get("门店名称") or item.get("门店") or item.get("name") or ""
            store_code = item.get("门店") or ""
            rec_id = item.get("record_id") or ""
            occurred_ms = int(item.get("销售发生时间")) if item.get("销售发生时间") else report_midnight_ms
            if name:
                if "销售" in item:
                    records.append({
                        "report_date": report_dt_cn.strftime("%Y-%m-%d"),
                        "period": "daily",
                        "entity_type": "store",
                        "entity_name": name,
                        "sales_wan": float(item.get("销售", 0)),
                        "occurred_at_ms": occurred_ms,
                        "store_code": store_code,
                        "record_id": rec_id,
                        "fetched_at_shanghai": fetched_at,
                    })
                if "月度累计销售" in item or "月度累计销售金额" in item:
                    month_val = item.get("月度累计销售金额", item.get("月度累计销售", 0))
                    records.append({
                        "report_date": report_dt_cn.strftime("%Y-%m-%d"),
                        "period": "monthly",
                        "entity_type": "store",
                        "entity_name": name,
                        "sales_wan": float(month_val),
                        "occurred_at_ms": occurred_ms,
                        "store_code": store_code,
                        "record_id": rec_id,
                        "fetched_at_shanghai": fetched_at,
                    })
                if "年度累计销售" in item or "年度累计销售金额" in item:
                    year_val = item.get("年度累计销售金额", item.get("年度累计销售", 0))
                    records.append({
                        "report_date": report_dt_cn.strftime("%Y-%m-%d"),
                        "period": "yearly",
                        "entity_type": "store",
                        "entity_name": name,
                        "sales_wan": float(year_val),
                        "occurred_at_ms": occurred_ms,
                        "store_code": store_code,
                        "record_id": rec_id,
                        "fetched_at_shanghai": fetched_at,
                    })
    else:
        specs = build_section_specs()
        blocks = slice_section_blocks(lines, specs)
        for spec, block_lines in blocks:
            rows = parse_block_rows(block_lines)
            for name, value in rows:
                records.append({
                    "report_date": report_dt_cn.strftime("%Y-%m-%d"),
                    "period": spec.period,
                    "entity_type": spec.entity_type,
                    "entity_name": name,
                    "sales_wan": value,
                    "occurred_at_ms": report_midnight_ms,
                    "store_code": "",
                    "record_id": "",
                    "fetched_at_shanghai": fetched_at,
                })

    return records


# --------------------------- output ---------------------------

def ensure_dir(p: Path) -> None:
    p.mkdir(parents=True, exist_ok=True)


def partition_dir_for(date_str: str) -> Path:
    y, m, d = date_str.split("-")
    return OUTPUT_DIR / y / m / d


def write_long_csv(records: list[dict]) -> Path:
    if not records:
        raise RuntimeError("No records to write")
    report_date = records[0]["report_date"]
    out_dir = partition_dir_for(report_date)
    ensure_dir(out_dir)
    out_path = out_dir / "sales.csv"
    fieldnames = [
        "report_date",
        "period",
        "entity_type",
        "entity_name",
        "sales_wan",
        "occurred_at_ms",
        "store_code",
        "record_id",
        "fetched_at_shanghai",
    ]
    with out_path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for rec in records:
            writer.writerow(rec)
    return out_path


def write_group_csv(records: list[dict]) -> Path:
    report_date = records[0]["report_date"]
    out_dir = partition_dir_for(report_date)
    ensure_dir(out_dir)
    out_path = out_dir / "sales_group.csv"
    fieldnames = [
        "report_date",
        "period",
        "sales_wan",
        "occurred_at_ms",
        "fetched_at_shanghai",
    ]
    with out_path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for rec in records:
            if rec["entity_type"] == "group":
                writer.writerow({k: rec[k] for k in fieldnames})
    return out_path


def write_business_type_csv(records: list[dict]) -> Path:
    report_date = records[0]["report_date"]
    out_dir = partition_dir_for(report_date)
    ensure_dir(out_dir)
    out_path = out_dir / "sales_business_type.csv"
    fieldnames = [
        "report_date",
        "period",
        "business_type",
        "sales_wan",
        "occurred_at_ms",
        "fetched_at_shanghai",
    ]
    with out_path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for rec in records:
            if rec["entity_type"] == "business_type":
                row = {
                    "report_date": rec["report_date"],
                    "period": rec["period"],
                    "business_type": rec["entity_name"],
                    "sales_wan": rec["sales_wan"],
                    "occurred_at_ms": rec["occurred_at_ms"],
                    "fetched_at_shanghai": rec["fetched_at_shanghai"],
                }
                writer.writerow(row)
    return out_path


def write_store_csv(records: list[dict]) -> Path:
    report_date = records[0]["report_date"]
    out_dir = partition_dir_for(report_date)
    ensure_dir(out_dir)
    out_path = out_dir / "sales_store.csv"
    fieldnames = [
        "report_date",
        "period",
        "store_name",
        "store_code",
        "record_id",
        "sales_wan",
        "occurred_at_ms",
        "fetched_at_shanghai",
    ]
    with out_path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for rec in records:
            if rec["entity_type"] == "store":
                row = {
                    "report_date": rec["report_date"],
                    "period": rec["period"],
                    "store_name": rec["entity_name"],
                    "store_code": rec["store_code"],
                    "record_id": rec["record_id"],
                    "sales_wan": rec["sales_wan"],
                    "occurred_at_ms": rec["occurred_at_ms"],
                    "fetched_at_shanghai": rec["fetched_at_shanghai"],
                }
                writer.writerow(row)
    return out_path


# --------------------------- markdown & svg ---------------------------

def format_markdown_table(headers: list[str], rows: list[list[str]]) -> str:
    lines = []
    lines.append("| " + " | ".join(headers) + " |")
    lines.append("| " + " | ".join(["---"] * len(headers)) + " |")
    for r in rows:
        lines.append("| " + " | ".join(r) + " |")
    return "\n".join(lines)


def topn(records: list[dict], entity_type: str, period: str, n: int = 10) -> list[dict]:
    rows = [r for r in records if r["entity_type"] == entity_type and r["period"] == period]
    rows_sorted = sorted(rows, key=lambda x: x["sales_wan"], reverse=True)[:n]
    return rows_sorted


def write_markdown_report(records: list[dict]) -> Path:
    report_date = records[0]["report_date"]
    out_dir = partition_dir_for(report_date)
    ensure_dir(out_dir)
    out_path = out_dir / "report.md"

    # Summary
    bt_all = [r for r in records if r["entity_type"] == "business_type"]
    st_all = [r for r in records if r["entity_type"] == "store"]
    bt_names = sorted({r["entity_name"] for r in bt_all})
    st_names = sorted({r["entity_name"] for r in st_all})
    bt_daily = [r for r in bt_all if r["period"] == "daily"]
    st_daily = [r for r in st_all if r["period"] == "daily"]
    bt_max = max(bt_daily, key=lambda x: x["sales_wan"]) if bt_daily else None
    bt_min = min(bt_daily, key=lambda x: x["sales_wan"]) if bt_daily else None
    st_max = max(st_daily, key=lambda x: x["sales_wan"]) if st_daily else None
    st_min = min(st_daily, key=lambda x: x["sales_wan"]) if st_daily else None

    summary_lines = [
        f"- 业态数: {len(bt_names)}",
        f"- 门店数: {len(st_names)}",
        f"- 业态日销最大: {bt_max['entity_name']} {bt_max['sales_wan']:.0f}" if bt_max else "- 业态日销最大: -",
        f"- 业态日销最小: {bt_min['entity_name']} {bt_min['sales_wan']:.0f}" if bt_min else "- 业态日销最小: -",
        f"- 门店日销最大: {st_max['entity_name']} {st_max['sales_wan']:.0f}" if st_max else "- 门店日销最大: -",
        f"- 门店日销最小: {st_min['entity_name']} {st_min['sales_wan']:.0f}" if st_min else "- 门店日销最小: -",
        "- 同比: -",
        "- 环比: -",
    ]

    # Group table
    group_rows = [r for r in records if r["entity_type"] == "group"]
    group_rows_sorted = sorted(group_rows, key=lambda x: ["daily", "monthly", "yearly"].index(x["period"]))
    group_tbl = format_markdown_table(
        ["period", "sales_wan"],
        [[g["period"], f"{g['sales_wan']}"] for g in group_rows_sorted],
    )

    # Top10 tables by period and entity
    bt_daily_top = topn(records, "business_type", "daily")
    bt_month_top = topn(records, "business_type", "monthly")
    bt_year_top = topn(records, "business_type", "yearly")
    st_daily_top = topn(records, "store", "daily")
    st_month_top = topn(records, "store", "monthly")
    st_year_top = topn(records, "store", "yearly")

    def rows_rank_name_value(items: list[dict]) -> list[list[str]]:
        return [[str(i + 1), it["entity_name"], f"{it['sales_wan']}"] for i, it in enumerate(items)]

    bt_daily_tbl = format_markdown_table(["rank", "business_type", "sales_wan"], rows_rank_name_value(bt_daily_top))
    bt_month_tbl = format_markdown_table(["rank", "business_type", "sales_wan"], rows_rank_name_value(bt_month_top))
    bt_year_tbl = format_markdown_table(["rank", "business_type", "sales_wan"], rows_rank_name_value(bt_year_top))

    st_daily_tbl = format_markdown_table(["rank", "store_name", "sales_wan"], rows_rank_name_value(st_daily_top))
    st_month_tbl = format_markdown_table(["rank", "store_name", "sales_wan"], rows_rank_name_value(st_month_top))
    st_year_tbl = format_markdown_table(["rank", "store_name", "sales_wan"], rows_rank_name_value(st_year_top))

    # SVGs
    svg_bu_daily = out_dir / "bu_daily_top10.svg"
    svg_bu_month = out_dir / "bu_monthly_top10.svg"
    svg_bu_year = out_dir / "bu_yearly_top10.svg"

    svg_st_daily = out_dir / "store_daily_top10.svg"
    svg_st_month = out_dir / "store_monthly_top10.svg"
    svg_st_year = out_dir / "store_yearly_top10.svg"

    try:
        write_bar_svg(svg_bu_daily, [(r["entity_name"], r["sales_wan"]) for r in bt_daily_top], title=f"业态日销TOP10 {report_date}")
        write_bar_svg(svg_bu_month, [(r["entity_name"], r["sales_wan"]) for r in bt_month_top], title=f"业态月度累计TOP10 {report_date}")
        write_bar_svg(svg_bu_year, [(r["entity_name"], r["sales_wan"]) for r in bt_year_top], title=f"业态年度累计TOP10 {report_date}")
        write_bar_svg(svg_st_daily, [(r["entity_name"], r["sales_wan"]) for r in st_daily_top], title=f"门店日销TOP10 {report_date}")
        write_bar_svg(svg_st_month, [(r["entity_name"], r["sales_wan"]) for r in st_month_top], title=f"门店月度累计TOP10 {report_date}")
        write_bar_svg(svg_st_year, [(r["entity_name"], r["sales_wan"]) for r in st_year_top], title=f"门店年度累计TOP10 {report_date}")
    except Exception:
        pass

    content = []
    content.append(f"# 销售日报 {report_date}")
    content.append("")
    content.append("## 摘要")
    content.append("")
    content.extend(summary_lines)
    content.append("")
    content.append("## 集团合计")
    content.append("")
    content.append(group_tbl)
    content.append("")

    content.append("## 业态 TOP10")
    content.append("")
    content.append("### 日销")
    content.append("")
    content.append(bt_daily_tbl)
    content.append("")
    content.append("![](./bu_daily_top10.svg)")
    content.append("")
    content.append("### 月度累计")
    content.append("")
    content.append(bt_month_tbl)
    content.append("")
    content.append("![](./bu_monthly_top10.svg)")
    content.append("")
    content.append("### 年度累计")
    content.append("")
    content.append(bt_year_tbl)
    content.append("")
    content.append("![](./bu_yearly_top10.svg)")
    content.append("")

    content.append("## 门店 TOP10")
    content.append("")
    content.append("### 日销")
    content.append("")
    content.append(st_daily_tbl)
    content.append("")
    content.append("![](./store_daily_top10.svg)")
    content.append("")
    content.append("### 月度累计")
    content.append("")
    content.append(st_month_tbl)
    content.append("")
    content.append("![](./store_monthly_top10.svg)")
    content.append("")
    content.append("### 年度累计")
    content.append("")
    content.append(st_year_tbl)
    content.append("")
    content.append("![](./store_yearly_top10.svg)")

    out_path.write_text("\n".join(content), encoding="utf-8")
    return out_path


def write_bar_svg(path: Path, data: list[tuple[str, float]], title: str = "") -> None:
    # Simple horizontal bar chart SVG
    width = 900
    bar_height = 24
    margin_left = 180
    margin_right = 40
    margin_top = 60
    margin_bottom = 40
    gap = 10

    n = len(data)
    chart_height = n * (bar_height + gap) - gap if n > 0 else 0
    height = margin_top + chart_height + margin_bottom

    max_val = max((v for _, v in data), default=0.0)
    scale = (width - margin_left - margin_right) / max_val if max_val > 0 else 1.0

    def esc(s: str) -> str:
        return (s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

    parts: list[str] = []
    parts.append(f"<svg xmlns='http://www.w3.org/2000/svg' width='{width}' height='{height}'>")
    parts.append("<style>text{font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, 'Helvetica Neue', Arial; font-size: 12px;} .title{font-size:16px;font-weight:bold}</style>")
    if title:
        parts.append(f"<text class='title' x='{margin_left}' y='30'>{esc(title)}</text>")

    y = margin_top
    for name, val in data:
        bar_w = int(val * scale)
        parts.append(f"<rect x='{margin_left}' y='{y}' width='{bar_w}' height='{bar_height}' fill='#155dfc' />")
        parts.append(f"<text x='{margin_left - 8}' y='{y + bar_height - 6}' text-anchor='end'>{esc(name)}</text>")
        parts.append(f"<text x='{margin_left + bar_w + 6}' y='{y + bar_height - 6}'>{val:.0f}</text>")
        y += bar_height + gap

    parts.append("</svg>")
    path.write_text("".join(parts), encoding="utf-8")


def write_all_csv(records: list[dict]) -> list[Path]:
    paths = [
        write_long_csv(records),
        write_group_csv(records),
        write_business_type_csv(records),
        write_store_csv(records),
    ]
    return paths


def write_all_outputs(records: list[dict]) -> list[Path]:
    files = []
    files.extend(write_all_csv(records))
    files.append(write_markdown_report(records))
    return files


if __name__ == "__main__":
    rows = scrape()
    paths = write_all_outputs(rows)
    for p in paths:
        print(f"Wrote: {p}")
    print(f"Total rows: {len(rows)}")
