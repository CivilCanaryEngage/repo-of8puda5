#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""A股板块主力行为 - 桌面客户端 (Tkinter)

展示字段: 板块, 涨幅, 成交额, 主力资金, 散户资金, 主力暗盘, 主力强度, 主力行为
支持行业/概念/地域板块切换、手动刷新、每日收盘后(15:30)自动刷新、导出CSV。
双击板块查看近2周历史；个股查询支持代码和中文名。
"""

import datetime
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from main_force import (COLUMNS, HIST_COLUMNS, SECTOR_FS, compute,
                        fetch_history, fetch_sectors, save_csv, search_stock)

KIND_NAMES = {"industry": "行业板块", "concept": "概念板块", "region": "地域板块"}
BEHAVIOR_COLORS = {"抢筹": "#d32f2f", "建仓": "#f57c00",
                   "洗盘": "#616161", "出货": "#2e7d32"}
AUTO_REFRESH_TIME = datetime.time(15, 30)  # 北京时间收盘后


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("A股板块主力行为")
        self.geometry("1000x640")
        self.rows = []
        self._build_ui()
        self._schedule_auto_refresh()
        self.refresh()

    def _build_ui(self):
        top = ttk.Frame(self, padding=8)
        top.pack(fill="x")

        ttk.Label(top, text="板块类型:").pack(side="left")
        self.kind_var = tk.StringVar(value="行业板块")
        kind_box = ttk.Combobox(top, textvariable=self.kind_var, width=10,
                                state="readonly",
                                values=list(KIND_NAMES.values()))
        kind_box.pack(side="left", padx=(4, 12))
        kind_box.bind("<<ComboboxSelected>>", lambda e: self.refresh())

        ttk.Label(top, text="行为筛选:").pack(side="left")
        self.filter_var = tk.StringVar(value="全部")
        filter_box = ttk.Combobox(top, textvariable=self.filter_var, width=8,
                                  state="readonly",
                                  values=["全部", "抢筹", "建仓", "洗盘", "出货"])
        filter_box.pack(side="left", padx=(4, 12))
        filter_box.bind("<<ComboboxSelected>>", lambda e: self._render())

        self.refresh_btn = ttk.Button(top, text="刷新", command=self.refresh)
        self.refresh_btn.pack(side="left")
        ttk.Button(top, text="导出CSV", command=self.export_csv).pack(
            side="left", padx=8)

        ttk.Label(top, text="个股查询:").pack(side="left", padx=(12, 0))
        self.stock_var = tk.StringVar()
        stock_entry = ttk.Entry(top, textvariable=self.stock_var, width=14)
        stock_entry.pack(side="left", padx=4)
        stock_entry.bind("<Return>", lambda e: self.query_stock())
        ttk.Button(top, text="查询", command=self.query_stock).pack(side="left")

        self.status_var = tk.StringVar(value="就绪")
        ttk.Label(top, textvariable=self.status_var).pack(side="right")

        info = ttk.Label(
            self, padding=(8, 0),
            text="主力暗盘=主力资金-散户资金; 主力强度=主力暗盘/成交额×100; "
                 "行为: ≥3 抢筹, [1,3) 建仓, (-1,1) 洗盘, ≤-1 出货; "
                 "每日15:30自动刷新; 双击板块查看近2周数据")
        info.pack(fill="x")

        frame = ttk.Frame(self, padding=8)
        frame.pack(fill="both", expand=True)
        self.tree = ttk.Treeview(frame, columns=COLUMNS, show="headings")
        for col in COLUMNS:
            self.tree.heading(
                col, text=col,
                command=lambda c=col: self._sort_by(c))
            width = 120 if col == "板块" else 90
            anchor = "w" if col == "板块" else "e"
            self.tree.column(col, width=width, anchor=anchor)
        vsb = ttk.Scrollbar(frame, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=vsb.set)
        self.tree.pack(side="left", fill="both", expand=True)
        vsb.pack(side="right", fill="y")
        for behavior, color in BEHAVIOR_COLORS.items():
            self.tree.tag_configure(behavior, foreground=color)
        self.tree.bind("<Double-1>", self._on_double_click)

        self._sort_col = "主力强度"
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
            rows = compute(fetch_sectors(kind))
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
        now = datetime.datetime.now().strftime("%H:%M:%S")
        self.status_var.set(f"共 {len(rows)} 个板块 | 更新于 {now}")
        self._render()

    def _render(self):
        self.tree.delete(*self.tree.get_children())
        behavior = self.filter_var.get()
        rows = [r for r in self.rows
                if behavior == "全部" or r["主力行为"] == behavior]
        rows.sort(key=lambda r: (r[self._sort_col]
                                 if isinstance(r[self._sort_col], (int, float))
                                 else 0),
                  reverse=self._sort_desc)
        if self._sort_col == "板块":
            rows.sort(key=lambda r: str(r["板块"]), reverse=self._sort_desc)
        self._iid_map = {}
        for r in rows:
            iid = self.tree.insert("", "end", values=[r[c] for c in COLUMNS],
                                   tags=(r["主力行为"],))
            self._iid_map[iid] = r

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
            self._open_history(row["_secid"], row["板块"])

    def query_stock(self):
        query = self.stock_var.get().strip()
        if not query:
            return
        self.status_var.set("搜索中...")

        def work():
            try:
                matches = search_stock(query)
                self.after(0, on_result, matches, None)
            except Exception as e:
                self.after(0, on_result, [], e)

        def on_result(matches, error):
            self.status_var.set("就绪")
            if error:
                messagebox.showerror("错误", f"搜索失败: {error}")
            elif not matches:
                messagebox.showinfo("提示", f"未找到个股: {query}")
            else:
                secid, code, name = matches[0]
                self._open_history(secid, f"{name}({code})")

        threading.Thread(target=work, daemon=True).start()

    def _open_history(self, secid, title):
        win = tk.Toplevel(self)
        win.title(f"{title} - 近2周主力行为")
        win.geometry("860x420")
        status = tk.StringVar(value="加载中...")
        ttk.Label(win, textvariable=status, padding=8).pack(fill="x")
        frame = ttk.Frame(win, padding=8)
        frame.pack(fill="both", expand=True)
        tree = ttk.Treeview(frame, columns=HIST_COLUMNS, show="headings")
        for col in HIST_COLUMNS:
            tree.heading(col, text=col)
            tree.column(col, width=100 if col == "日期" else 90,
                        anchor="w" if col == "日期" else "e")
        vsb = ttk.Scrollbar(frame, orient="vertical", command=tree.yview)
        tree.configure(yscrollcommand=vsb.set)
        tree.pack(side="left", fill="both", expand=True)
        vsb.pack(side="right", fill="y")
        for behavior, color in BEHAVIOR_COLORS.items():
            tree.tag_configure(behavior, foreground=color)

        def work():
            try:
                rows = fetch_history(secid)
                win.after(0, on_data, rows, None)
            except Exception as e:
                win.after(0, on_data, [], e)

        def on_data(rows, error):
            if not win.winfo_exists():
                return
            if error:
                status.set(f"加载失败: {error}")
                return
            status.set(f"{title} | 近 {len(rows)} 个交易日 (日期降序)")
            for r in rows:
                tree.insert("", "end", values=[r[c] for c in HIST_COLUMNS],
                            tags=(r["主力行为"],))

        threading.Thread(target=work, daemon=True).start()

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


if __name__ == "__main__":
    App().mainloop()
