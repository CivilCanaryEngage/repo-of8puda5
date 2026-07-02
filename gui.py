#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""A股板块主力行为 - 桌面客户端 (Tkinter)

展示字段: 板块, 涨幅, 5日涨幅, 成交额, 主力资金, 散户资金, 主力暗盘,
主力强度, 5日主力占比, 主力行为(多日复合判别)。
交互:
    - 行业/概念/地域板块切换, 行为筛选, 表头点击排序(带▲▼指示)
    - 搜索框支持个股/板块(代码/中文/拼音), 回车直达; 多个匹配时弹出选择
    - 双击任意行查看历史, 历史窗口可切换近7天/近14天
    - F5 手动刷新; 每日收盘后(15:30)自动刷新; 导出CSV
"""

import datetime
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from main_force import (COLUMNS, HIST_COLUMNS, compute, detect_movers,
                        fetch_history_full, fetch_indices, fetch_sectors,
                        save_csv, search_security)

KIND_NAMES = {"industry": "行业板块", "concept": "概念板块", "region": "地域板块"}
BEHAVIOR_COLORS = {"抢筹": "#d32f2f", "建仓": "#f57c00",
                   "洗盘": "#616161", "出货": "#2e7d32"}
PINNED_BG = "#eef3fb"
STRIPE_BG = "#f7f7f7"
ALERT_BG = "#fdecec"  # 行为刚切换为抢筹的行
AUTO_REFRESH_TIME = datetime.time(15, 30)  # 北京时间收盘后


def _column_width(col):
    if col == "板块":
        return 130
    if col in ("主力行为", "主力强度"):
        return 76
    return 92


def _fill_tree(tree, columns, rows, iid_map=None, pinned_key=None):
    """向 Treeview 写入行: 行为着色 + 斑马纹 + 置顶底色。"""
    tree.delete(*tree.get_children())
    for i, r in enumerate(rows):
        tags = [r["主力行为"]]
        if pinned_key and r.get(pinned_key):
            tags.append("pinned")
        elif r.get("_alert"):
            tags.append("alert")
        elif i % 2:
            tags.append("stripe")
        iid = tree.insert("", "end", values=[r.get(c, "") for c in columns],
                          tags=tuple(tags))
        if iid_map is not None:
            iid_map[iid] = r


def _make_tree(parent, columns, heading_cmd=None):
    frame = ttk.Frame(parent, padding=(8, 4, 8, 8))
    frame.pack(fill="both", expand=True)
    tree = ttk.Treeview(frame, columns=columns, show="headings")
    for col in columns:
        if heading_cmd:
            tree.heading(col, text=col, command=lambda c=col: heading_cmd(c))
        else:
            tree.heading(col, text=col)
        anchor = "w" if col in ("板块", "日期") else "e"
        tree.column(col, width=_column_width(col), anchor=anchor)
    vsb = ttk.Scrollbar(frame, orient="vertical", command=tree.yview)
    hsb = ttk.Scrollbar(frame, orient="horizontal", command=tree.xview)
    tree.configure(yscrollcommand=vsb.set, xscrollcommand=hsb.set)
    tree.grid(row=0, column=0, sticky="nsew")
    vsb.grid(row=0, column=1, sticky="ns")
    hsb.grid(row=1, column=0, sticky="ew")
    frame.rowconfigure(0, weight=1)
    frame.columnconfigure(0, weight=1)
    for behavior, color in BEHAVIOR_COLORS.items():
        tree.tag_configure(behavior, foreground=color)
    tree.tag_configure("pinned", background=PINNED_BG)
    tree.tag_configure("stripe", background=STRIPE_BG)
    tree.tag_configure("alert", background=ALERT_BG)
    return tree


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("A股板块主力行为")
        self.geometry("1180x680")
        ttk.Style(self).theme_use("clam")
        self.rows = []
        self._last_behavior = {}  # 上次刷新的 {板块: 行为}, 用于变化提醒
        self._build_ui()
        self._schedule_auto_refresh()
        self.refresh()

    def _build_ui(self):
        top = ttk.Frame(self, padding=8)
        top.pack(fill="x")

        ttk.Label(top, text="板块:").pack(side="left")
        self.kind_var = tk.StringVar(value="行业板块")
        kind_box = ttk.Combobox(top, textvariable=self.kind_var, width=9,
                                state="readonly",
                                values=list(KIND_NAMES.values()))
        kind_box.pack(side="left", padx=(4, 10))
        kind_box.bind("<<ComboboxSelected>>", lambda e: self.refresh())

        ttk.Label(top, text="行为:").pack(side="left")
        self.filter_var = tk.StringVar(value="全部")
        filter_box = ttk.Combobox(top, textvariable=self.filter_var, width=7,
                                  state="readonly",
                                  values=["全部", "抢筹", "建仓", "洗盘", "出货"])
        filter_box.pack(side="left", padx=(4, 10))
        filter_box.bind("<<ComboboxSelected>>", lambda e: self._render())

        self.refresh_btn = ttk.Button(top, text="刷新(F5)", command=self.refresh)
        self.refresh_btn.pack(side="left")
        ttk.Button(top, text="主力异动榜", command=self.show_movers).pack(
            side="left", padx=(8, 0))
        ttk.Button(top, text="导出CSV", command=self.export_csv).pack(
            side="left", padx=8)

        ttk.Label(top, text="搜索个股/板块:").pack(side="left", padx=(10, 0))
        self.stock_var = tk.StringVar()
        stock_entry = ttk.Entry(top, textvariable=self.stock_var, width=16)
        stock_entry.pack(side="left", padx=4)
        stock_entry.bind("<Return>", lambda e: self.query_stock())
        ttk.Button(top, text="查询", command=self.query_stock).pack(side="left")
        self.bind("<F5>", lambda e: self.refresh())

        info = ttk.Label(
            self, padding=(8, 0), foreground="#666",
            text="主力=超大单+大单, 散户=小单; 暗盘=主力-散户; 强度=暗盘/成交额×100; "
                 "行为=多日复合判别(当日强度×5日主力占比×涨幅×散户方向); "
                 "指数置顶, 其余按成交额排序; 双击查看近7/14天数据")
        info.pack(fill="x")

        self.tree = _make_tree(self, COLUMNS, heading_cmd=self._sort_by)
        self.tree.bind("<Double-1>", self._on_double_click)

        status_bar = ttk.Frame(self, padding=(8, 2))
        status_bar.pack(fill="x", side="bottom")
        self.status_var = tk.StringVar(value="就绪")
        ttk.Label(status_bar, textvariable=self.status_var).pack(side="left")
        ttk.Label(status_bar, foreground="#999",
                  text="每日15:30自动刷新").pack(side="right")

        self._sort_col = "成交额(亿)"  # 市场热度
        self._sort_desc = True

    def _kind(self):
        name = self.kind_var.get()
        return {v: k for k, v in KIND_NAMES.items()}[name]

    def refresh(self):
        self.refresh_btn.state(["disabled"])
        self.status_var.set("加载中...")
        kind = self._kind()
        threading.Thread(target=self._fetch, args=(kind,), daemon=True).start()

    def _fetch(self, kind):
        try:
            rows = (compute(fetch_indices(), pinned=True)
                    + compute(fetch_sectors(kind)))
            self.after(0, self._on_data, rows, None)
        except Exception as e:
            self.after(0, self._on_data, [], e)

    def _on_data(self, rows, error):
        self.refresh_btn.state(["!disabled"])
        if error:
            self.status_var.set("加载失败")
            messagebox.showerror("错误", f"获取数据失败: {error}")
            return
        self.rows = rows
        alerts = self._mark_behavior_changes(rows)
        now = datetime.datetime.now().strftime("%H:%M:%S")
        status = f"共 {len(rows)} 个板块 | 更新于 {now}"
        if alerts:
            names = "、".join(alerts[:5]) + ("等" if len(alerts) > 5 else "")
            status += f" | ⚡ {len(alerts)} 个板块转为抢筹: {names}"
            self.bell()
        self.status_var.set(status)
        self._render()

    def _mark_behavior_changes(self, rows):
        """标记本次刷新中行为切换为抢筹的板块(红底高亮)。"""
        alerts = []
        for r in rows:
            prev = self._last_behavior.get(r["板块"])
            if (not r.get("_pinned") and prev
                    and prev != "抢筹" and r["主力行为"] == "抢筹"):
                r["_alert"] = True
                alerts.append(r["板块"])
        self._last_behavior = {r["板块"]: r["主力行为"] for r in rows}
        return alerts

    def show_movers(self):
        """主力异动榜: 多日建仓后当日转抢筹的板块。"""
        if not self.rows:
            messagebox.showinfo("提示", "暂无数据, 请先刷新")
            return
        MoversWindow(self, detect_movers(self.rows))

    def _render(self):
        behavior = self.filter_var.get()
        pinned = [r for r in self.rows if r.get("_pinned")]
        rows = [r for r in self.rows if not r.get("_pinned")
                and (behavior == "全部" or r["主力行为"] == behavior)]
        if self._sort_col == "板块":
            rows.sort(key=lambda r: str(r["板块"]), reverse=self._sort_desc)
        else:
            rows.sort(key=lambda r: (r[self._sort_col]
                                     if isinstance(r[self._sort_col],
                                                   (int, float)) else 0),
                      reverse=self._sort_desc)
        for col in COLUMNS:  # 表头排序指示
            arrow = ""
            if col == self._sort_col:
                arrow = " ▼" if self._sort_desc else " ▲"
            self.tree.heading(col, text=col + arrow)
        self._iid_map = {}
        _fill_tree(self.tree, COLUMNS, pinned + rows,
                   iid_map=self._iid_map, pinned_key="_pinned")

    def _sort_by(self, col):
        if self._sort_col == col:
            self._sort_desc = not self._sort_desc
        else:
            self._sort_col, self._sort_desc = col, True
        self._render()

    def export_csv(self):
        if not self.rows:
            messagebox.showinfo("提示", "暂无数据")
            return
        date_str = datetime.date.today().isoformat()
        path = filedialog.asksaveasfilename(
            defaultextension=".csv",
            initialfile=f"{date_str}_{self._kind()}.csv",
            filetypes=[("CSV 文件", "*.csv")])
        if path:
            save_csv(self.rows, path)
            messagebox.showinfo("提示", f"已导出: {path}")

    def _on_double_click(self, event):
        iid = self.tree.identify_row(event.y)
        row = getattr(self, "_iid_map", {}).get(iid)
        if row:
            HistoryWindow(self, row["_secid"], row["板块"])

    def query_stock(self):
        query = self.stock_var.get().strip()
        if not query:
            return
        self.status_var.set("搜索中...")

        def work():
            try:
                matches = search_security(query)
                self.after(0, on_result, matches, None)
            except Exception as e:
                self.after(0, on_result, [], e)

        def on_result(matches, error):
            self.status_var.set("就绪")
            if error:
                messagebox.showerror("错误", f"搜索失败: {error}")
            elif not matches:
                messagebox.showinfo("提示", f"未找到个股或板块: {query}")
            elif len(matches) == 1:
                secid, code, name = matches[0]
                HistoryWindow(self, secid, f"{name}({code})")
            else:
                self._pick_match(matches)

        threading.Thread(target=work, daemon=True).start()

    def _pick_match(self, matches):
        """多个匹配时弹出选择列表, 双击或回车确定。"""
        win = tk.Toplevel(self)
        win.title("请选择")
        win.geometry("300x260")
        win.transient(self)
        ttk.Label(win, text="找到多个匹配:", padding=8).pack(fill="x")
        box = tk.Listbox(win)
        for secid, code, name in matches:
            box.insert("end", f"{name} ({code})")
        box.pack(fill="both", expand=True, padx=8, pady=(0, 8))
        box.selection_set(0)
        box.focus_set()

        def choose(_=None):
            sel = box.curselection()
            if sel:
                secid, code, name = matches[sel[0]]
                win.destroy()
                HistoryWindow(self, secid, f"{name}({code})")

        box.bind("<Double-1>", choose)
        box.bind("<Return>", choose)

    def _schedule_auto_refresh(self):
        now = datetime.datetime.now()
        target = datetime.datetime.combine(now.date(), AUTO_REFRESH_TIME)
        if now >= target:
            target += datetime.timedelta(days=1)
        delay_ms = int((target - now).total_seconds() * 1000)
        self.after(delay_ms, self._auto_refresh)

    def _auto_refresh(self):
        if datetime.date.today().weekday() < 5:  # 仅交易日(周一至周五)
            self.refresh()
        self._schedule_auto_refresh()


class MoversWindow(tk.Toplevel):
    """主力异动榜: 当日抢筹且近5日主力占比>=1(前期持续吸筹)。"""

    def __init__(self, master, movers):
        super().__init__(master)
        self.title("主力异动榜")
        self.geometry("1080x420")
        ttk.Label(
            self, padding=8,
            text=f"共 {len(movers)} 个板块: 前期多日持续吸筹(5日主力占比≥1) "
                 "且当日放量抢筹 —— 典型启动信号, 按当日强度降序; 双击查看历史"
        ).pack(fill="x")
        self.tree = _make_tree(self, COLUMNS)
        self._iid_map = {}
        _fill_tree(self.tree, COLUMNS, movers, iid_map=self._iid_map)
        self.tree.bind("<Double-1>", self._on_double_click)

    def _on_double_click(self, event):
        row = self._iid_map.get(self.tree.identify_row(event.y))
        if row:
            HistoryWindow(self, row["_secid"], row["板块"])


class HistoryWindow(tk.Toplevel):
    """近 N 天主力行为历史窗口, 支持近7天/近14天切换。"""

    def __init__(self, master, secid, title):
        super().__init__(master)
        self.secid = secid
        self.name = title
        self.all_rows = None  # 一次拉取14天, 切换时本地截取
        self.title(f"{title} - 主力行为历史")
        self.geometry("980x430")

        top = ttk.Frame(self, padding=8)
        top.pack(fill="x")
        self.days_var = tk.IntVar(value=14)
        for days in (7, 14):
            ttk.Radiobutton(top, text=f"近{days}天", value=days,
                            variable=self.days_var,
                            command=self._render).pack(side="left",
                                                       padx=(0, 8))
        self.status = tk.StringVar(value="加载中...")
        ttk.Label(top, textvariable=self.status).pack(side="right")

        self.tree = _make_tree(self, HIST_COLUMNS)
        threading.Thread(target=self._work, daemon=True).start()

    def _work(self):
        try:
            rows, degraded = fetch_history_full(self.secid, days=14)
            self.after(0, self._on_data, rows, degraded, None)
        except Exception as e:
            self.after(0, self._on_data, [], False, e)

    def _on_data(self, rows, degraded, error):
        if not self.winfo_exists():
            return
        if error:
            self.status.set(f"加载失败: {error}")
            return
        self.all_rows = rows
        self.degraded = degraded
        self._render()

    def _render(self):
        if self.all_rows is None:
            return
        rows = self.all_rows[:self.days_var.get()]
        _fill_tree(self.tree, HIST_COLUMNS, rows)
        main_sum = sum(r["主力资金(亿)"] for r in rows)
        dark_sum = sum(r["主力暗盘(亿)"] for r in rows)
        prefix = ("⚠ 接口限流, 仅当日数据, 稍后重新打开可取完整历史 | "
                  if getattr(self, "degraded", False) else "")
        self.status.set(
            f"{prefix}{self.name} | 近 {len(rows)} 个交易日 | "
            f"累计主力净流入 {main_sum:.2f}亿, 累计暗盘 {dark_sum:.2f}亿")


if __name__ == "__main__":
    App().mainloop()
