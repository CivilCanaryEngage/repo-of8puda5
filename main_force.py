#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""A股板块主力行为计算工具

数据来源: 东方财富板块资金流接口(免费公开)
口径:
    主力资金 = 超大单+大单净流入(机构/大户)
    散户资金 = 小单净流入(中单视为中户/游资, 不计入散户,
               避免资金流零和导致散户恒等于-主力)
公式:
    主力暗盘 = 主力资金 - 散户资金
    主力强度 = 主力暗盘 / 成交额 * 100
主力行为(多日复合判别: 资金方向 × 价格表现 × 散户方向):
    抢筹: 当日强力流入且放量上攻 (强度>=3 且 涨幅>0)
    出货: 主力撤离且散户接盘或多日持续流出 (强度<=-1 且 (散户流入 或 5日占比<=-1))
    建仓: 多日持续吸筹且当日未明显流出 (5日主力净占比>=1 且 强度>-1)
    洗盘: 资金平衡+回调+散户离场 (|强度|<1 且 涨幅<=0 且 散户流出)
    其余按单日强度兜底: >=3 抢筹 / [1,3) 建仓 / (-1,1) 洗盘 / <=-1 出货
"""

import argparse
import csv
import datetime
import json
import os
import time
import urllib.parse
import urllib.request

API = (
    "https://push2delay.eastmoney.com/api/qt/clist/get"
    "?pn={page}&pz=100&po=1&np=1&fltt=2&invt=2&fid=f62&fs={fs}"
    "&fields=f12,f13,f14,f3,f6,f62,f66,f72,f78,f84,f109,f164,f165"
)

# 固定置顶的大盘指数: 上证指数 / 创业板指 / 科创50
INDEX_SECIDS = "1.000001,0.399006,1.000688"

INDEX_API = (
    "https://push2delay.eastmoney.com/api/qt/ulist.np/get"
    "?fltt=2&invt=2&secids={secids}"
    "&fields=f12,f13,f14,f3,f6,f62,f66,f72,f78,f84,f109,f164,f165"
)

# fs 参数: 行业板块 m:90+t:2, 概念板块 m:90+t:3, 地域板块 m:90+t:1
SECTOR_FS = {
    "industry": "m:90+t:2",
    "concept": "m:90+t:3",
    "region": "m:90+t:1",
}

HEADERS = {"User-Agent": "Mozilla/5.0"}

# push2his 提供历史数据; push2delay 仅返回当日, 作为降级备选
HIST_FFLOW_HOSTS = ["push2his.eastmoney.com", "push2delay.eastmoney.com"]

HIST_FFLOW_API = (
    "https://{host}/api/qt/stock/fflow/daykline/get"
    "?lmt={days}&klt=101&secid={secid}&fields1=f1,f2,f3,f7"
    "&fields2=f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61,f62,f63"
)

SUGGEST_API = (
    "https://searchapi.eastmoney.com/api/suggest/get"
    "?input={query}&type=14&count=10"
)


def _get_json(url, retries=3):
    req = urllib.request.Request(url, headers=HEADERS)
    for attempt in range(retries):
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except Exception:
            if attempt == retries - 1:
                raise
            time.sleep(1)


def fetch_sectors(kind: str = "industry"):
    fs = SECTOR_FS[kind]
    rows, page, total = [], 1, None
    while total is None or len(rows) < total:
        url = API.format(page=page, fs=fs)
        req = urllib.request.Request(url, headers=HEADERS)
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        d = data.get("data") or {}
        total = d.get("total", 0)
        diff = d.get("diff") or []
        if not diff:
            break
        rows.extend(diff)
        page += 1
    return rows


def fetch_indices():
    data = _get_json(INDEX_API.format(secids=INDEX_SECIDS))
    return ((data.get("data") or {}).get("diff")) or []


def search_security(query: str):
    """搜索个股/板块(支持代码/中文/拼音)，返回 [(secid, 代码, 名称), ...]"""
    url = SUGGEST_API.format(query=urllib.parse.quote(query))
    data = _get_json(url)
    items = ((data.get("QuotationCodeTable") or {}).get("Data")) or []
    return [(it["QuoteID"], it["Code"], it["Name"])
            for it in items if it.get("Classify") in ("AStock", "BK")]


def fetch_history(secid: str, days: int = 14):
    """获取近 N 个交易日的资金流历史, secid 如 90.BK0478 / 1.600519。

    返回按日期降序的字典列表, 字段同 HIST_COLUMNS。
    为了计算每日的"5日主力占比", 实际多拉取 4 日作为滚动窗口。
    """
    fflow = None
    for host in HIST_FFLOW_HOSTS:
        try:
            fflow = _get_json(
                HIST_FFLOW_API.format(host=host, secid=secid, days=days + 4),
                retries=2)
            break
        except Exception:
            if host == HIST_FFLOW_HOSTS[-1]:
                raise
    daily = []
    for line in ((fflow.get("data") or {}).get("klines")) or []:
        # f51日期, f52主力, f53小单, f54中单, f55大单, f56超大单,
        # f57~f61对应各类单净占比(%), f62收盘价, f63涨跌幅(%)
        p = line.split(",")
        main = float(p[1])
        retail = float(p[2])  # 散户资金(仅小单)
        # 接口不直接返回成交额, 由 净额/净占比×100 反推;
        # 取净占比绝对值最大的一类单(主力/小/中/大/超大)以降低舍入误差
        pairs = [(float(p[i]), float(p[i + 5])) for i in range(1, 6)]
        value, ratio = max(pairs, key=lambda vr: abs(vr[1]))
        if not ratio:
            continue
        daily.append({"date": p[0], "pct": float(p[12]), "main": main,
                      "retail": retail, "amount": value / ratio * 100})
    daily.sort(key=lambda x: x["date"])
    result = []
    for i, d in enumerate(daily):
        window = daily[max(0, i - 4):i + 1]  # 滚动5日窗口
        amt5 = sum(w["amount"] for w in window)
        s5 = sum(w["main"] for w in window) / amt5 * 100 if amt5 else None
        dark = d["main"] - d["retail"]
        strength = dark / d["amount"] * 100
        result.append({
            "日期": d["date"],
            "涨幅(%)": d["pct"],
            "成交额(亿)": round(d["amount"] / 1e8, 2),
            "主力资金(亿)": round(d["main"] / 1e8, 2),
            "散户资金(亿)": round(d["retail"] / 1e8, 2),
            "主力暗盘(亿)": round(dark / 1e8, 2),
            "主力强度": round(strength, 2),
            "5日主力占比": round(s5, 2) if s5 is not None else "",
            "主力行为": classify(strength, d["pct"], d["retail"], s5),
        })
    result.sort(key=lambda x: x["日期"], reverse=True)
    return result[:days]


HIST_COLUMNS = ["日期", "涨幅(%)", "成交额(亿)", "主力资金(亿)",
                "散户资金(亿)", "主力暗盘(亿)", "主力强度",
                "5日主力占比", "主力行为"]


def classify(strength, pct=None, retail=None, s5=None):
    """多日复合判别主力行为(资金方向 × 价格表现 × 散户方向)。

    strength: 当日主力强度; pct: 当日涨幅%; retail: 散户资金(元, 取方向);
    s5: 近5日主力净占比%(无多日数据时为 None, 退化为单日判别)。
    """
    if pct is not None:
        if strength >= 3 and pct > 0:
            return "抢筹"
        if strength <= -1 and ((retail or 0) > 0
                               or (s5 is not None and s5 <= -1)):
            return "出货"
        if s5 is not None and s5 >= 1 and strength > -1:
            return "建仓"
        if abs(strength) < 1 and pct <= 0 and (retail or 0) <= 0:
            return "洗盘"
    # 兑底: 按单日强度
    if strength >= 3:
        return "抢筹"
    if strength >= 1:
        return "建仓"
    if strength > -1:
        return "洗盘"
    return "出货"


def compute(rows, pinned=False):
    result = []
    for r in rows:
        name = r.get("f14")
        pct = r.get("f3")            # 涨幅 %
        amount = r.get("f6")         # 成交额 元
        main = r.get("f62")          # 主力资金净流入(超大单+大单) 元
        small = r.get("f84") or 0    # 小单净流入
        if not isinstance(amount, (int, float)) or not amount:
            continue
        if not isinstance(main, (int, float)):
            continue
        retail = small               # 散户资金(仅小单)
        dark = main - retail         # 主力暗盘
        strength = dark / amount * 100
        pct5 = r.get("f109")         # 5日涨跌幅 %
        s5 = r.get("f165")           # 5日主力净占比 %
        if not isinstance(s5, (int, float)):
            s5 = None
        result.append({
            "_secid": f"{r.get('f13', 90)}.{r.get('f12')}",
            "_pinned": pinned,
            "板块": name,
            "涨幅(%)": pct,
            "5日涨幅(%)": pct5 if isinstance(pct5, (int, float)) else "",
            "成交额(亿)": round(amount / 1e8, 2),
            "主力资金(亿)": round(main / 1e8, 2),
            "散户资金(亿)": round(retail / 1e8, 2),
            "主力暗盘(亿)": round(dark / 1e8, 2),
            "主力强度": round(strength, 2),
            "5日主力占比": s5 if s5 is not None else "",
            "主力行为": classify(
                strength, pct if isinstance(pct, (int, float)) else None,
                retail, s5),
        })
    if not pinned:
        # 按市场热度(成交额)降序
        result.sort(key=lambda x: x["成交额(亿)"], reverse=True)
    return result


COLUMNS = ["板块", "涨幅(%)", "5日涨幅(%)", "成交额(亿)", "主力资金(亿)",
           "散户资金(亿)", "主力暗盘(亿)", "主力强度",
           "5日主力占比", "主力行为"]


def print_table(rows, limit=None):
    def w(s):
        return sum(2 if ord(c) > 127 else 1 for c in str(s))

    def pad(s, width):
        return str(s) + " " * (width - w(s))

    shown = rows[:limit] if limit else rows
    widths = [max([w(c)] + [w(r[c]) for r in shown]) for c in COLUMNS]
    print("  ".join(pad(c, widths[i]) for i, c in enumerate(COLUMNS)))
    print("-" * (sum(widths) + 2 * (len(COLUMNS) - 1)))
    for r in shown:
        print("  ".join(pad(r[c], widths[i]) for i, c in enumerate(COLUMNS)))


def save_csv(rows, path):
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=COLUMNS, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def save_html(rows, path, kind, date_str):
    behavior_color = {"抢筹": "#d32f2f", "建仓": "#f57c00",
                      "洗盘": "#616161", "出货": "#2e7d32"}
    kind_name = {"industry": "行业板块", "concept": "概念板块",
                 "region": "地域板块"}[kind]
    trs = []
    for r in rows:
        pct = r["涨幅(%)"]
        pct_color = "#d32f2f" if isinstance(pct, (int, float)) and pct >= 0 else "#2e7d32"
        tds = [
            f"<td>{r['板块']}</td>",
            f"<td style='color:{pct_color}'>{pct}</td>",
            f"<td>{r['5日涨幅(%)']}</td>",
            f"<td>{r['成交额(亿)']}</td>",
            f"<td>{r['主力资金(亿)']}</td>",
            f"<td>{r['散户资金(亿)']}</td>",
            f"<td>{r['主力暗盘(亿)']}</td>",
            f"<td>{r['主力强度']}</td>",
            f"<td>{r['5日主力占比']}</td>",
            f"<td style='color:{behavior_color[r['主力行为']]};font-weight:bold'>{r['主力行为']}</td>",
        ]
        trs.append("<tr>" + "".join(tds) + "</tr>")
    html = f"""<!DOCTYPE html>
<html lang="zh"><head><meta charset="utf-8">
<title>{date_str} {kind_name}主力行为</title>
<style>
body{{font-family:sans-serif;margin:20px}}
table{{border-collapse:collapse;width:100%}}
th,td{{border:1px solid #ccc;padding:6px 10px;text-align:right;white-space:nowrap}}
th{{background:#f5f5f5}}
td:first-child,th:first-child{{text-align:left}}
tr:hover{{background:#fafafa}}
</style></head><body>
<h2>{date_str} {kind_name}主力行为 (指数置顶, 其余按成交额降序)</h2>
<p>主力暗盘=主力资金-散户资金; 主力强度=主力暗盘/成交额×100;
行为为多日复合判别: 抢筹=当日强力流入且上涨; 出货=主力撤离且散户接盘/多日流出;
建仓=5日持续吸筹; 洗盘=资金平衡+回调+散户离场</p>
<table><thead><tr>{"".join(f"<th>{c}</th>" for c in COLUMNS)}</tr></thead>
<tbody>{"".join(trs)}</tbody></table>
</body></html>"""
    with open(path, "w", encoding="utf-8") as f:
        f.write(html)


def main():
    parser = argparse.ArgumentParser(description="A股板块主力行为计算工具")
    parser.add_argument("--kind", choices=list(SECTOR_FS), default="industry",
                        help="板块类型: industry 行业(默认) / concept 概念 / region 地域")
    parser.add_argument("--top", type=int, default=0,
                        help="终端只显示前 N 条(0=全部)")
    parser.add_argument("--outdir", default="output", help="报告输出目录")
    args = parser.parse_args()

    date_str = datetime.date.today().isoformat()
    rows = compute(fetch_indices(), pinned=True) + compute(
        fetch_sectors(args.kind))
    if not rows:
        raise SystemExit("未获取到数据，请检查网络或接口。")

    print_table(rows, args.top or None)

    os.makedirs(args.outdir, exist_ok=True)
    base = os.path.join(args.outdir, f"{date_str}_{args.kind}")
    save_csv(rows, base + ".csv")
    save_html(rows, base + ".html", args.kind, date_str)
    print(f"\n共 {len(rows)} 个板块，报告已保存: {base}.csv / {base}.html")


if __name__ == "__main__":
    main()
