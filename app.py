"""Chinese, offline Tk GUI. Processing runs on one worker; widgets on UI thread."""
from __future__ import annotations
import json
import os
from pathlib import Path
import queue
import sys
import threading
import tkinter as tk
from tkinter import ttk, filedialog, messagebox
import traceback
from tkinterdnd2 import TkinterDnD, DND_FILES
import export_core as core


class App:
    def __init__(self, root):
        self.root = root
        root.title("照片高清导出器 · RAW / JPG")
        root.geometry("960x790")
        root.minsize(860, 730)
        root.configure(bg="#f3f5f8")
        self.events = queue.Queue()
        self.control = core.Control()
        self.busy = False
        self.photos = []
        self.precheck = None
        self.last_output = None
        self.errors = []
        self.input = tk.StringVar()
        self.output = tk.StringVar()
        self.selection = tk.StringVar(value="all")
        self.mode = tk.StringVar(value="wechat")
        self.long_edge = tk.StringVar(value="2560")
        self.quality = tk.StringVar(value="95")
        self.minimum = tk.StringVar(value="88")
        self.cap = tk.StringVar(value="4")
        self.sharpen = tk.BooleanVar(value=False)
        self.keep_exif = tk.BooleanVar(value=True)
        self.structure = tk.BooleanVar(value=True)
        self.below = tk.StringVar(value="保留尺寸并标记")
        self.existing = tk.StringVar(value="验证后断点续跑")
        self.orientation = tk.StringVar(value="自动判断")
        self.only_normal = tk.BooleanVar(value=False)
        self.status = tk.StringVar(value="选择或拖入照片文件夹，选择输出目录，然后预检或开始。")
        self.scan_info = tk.StringVar(value="尚未扫描")
        self.mode_note = tk.StringVar()
        self.counts = tk.StringVar(value="总数 0　已完成 0　成功 0　跳过 0　失败 0")
        self.speed = tk.StringVar(value="速度 —　预计剩余 —")
        self.current = tk.StringVar(value="等待开始")
        self.preflight_info = tk.StringVar(value="预检仅检查 JPEG 头与元数据；完整性会在正式导出时验证。")
        self.style()
        self.build()
        self.set_preset()
        root.protocol("WM_DELETE_WINDOW", self.close)
        root.after(100, self.poll)

    def style(self):
        style = ttk.Style()
        style.theme_use("clam")
        style.configure("TFrame", background="#f3f5f8")
        style.configure("Card.TFrame", background="white")
        style.configure("TLabel", background="#f3f5f8", foreground="#253244", font=("Microsoft YaHei UI", 10))
        style.configure("Card.TLabel", background="white")
        style.configure("Title.TLabel", font=("Microsoft YaHei UI", 21, "bold"))
        style.configure("Sub.TLabel", foreground="#65738a")
        style.configure("TButton", font=("Microsoft YaHei UI", 10), padding=(12, 7))
        style.configure("Accent.TButton", background="#1769e0", foreground="white")
        style.map("Accent.TButton", background=[("active", "#125bc3"), ("disabled", "#a6bad6")])
        style.configure("TEntry", padding=6)
        style.configure("TRadiobutton", background="#f3f5f8", font=("Microsoft YaHei UI", 10))
        style.configure("TCheckbutton", background="#f3f5f8", font=("Microsoft YaHei UI", 10))

    def build(self):
        outer = ttk.Frame(self.root, padding=(24, 20))
        outer.pack(fill="both", expand=True)
        ttk.Label(outer, text="让照片直接可分享", style="Title.TLabel").pack(anchor="w")
        ttk.Label(outer, text="RAW 只提取机内 JPEG · 支持已有 JPG · 本机离线处理 · 原照片只读", style="Sub.TLabel").pack(anchor="w", pady=(4, 16))
        paths = ttk.Frame(outer)
        paths.pack(fill="x")
        ttk.Label(paths, text="照片文件夹").grid(row=0, column=0, sticky="w", padx=(0, 12))
        self.input_entry = ttk.Entry(paths, textvariable=self.input)
        self.input_entry.grid(row=0, column=1, sticky="ew", pady=4)
        self.input_entry.drop_target_register(DND_FILES)
        self.input_entry.dnd_bind("<<Drop>>", self.drop)
        self.input_button = ttk.Button(paths, text="选择 / 拖入", command=self.choose_input)
        self.input_button.grid(row=0, column=2, padx=(8, 0))
        ttk.Label(paths, text="输出文件夹").grid(row=1, column=0, sticky="w")
        self.output_entry = ttk.Entry(paths, textvariable=self.output)
        self.output_entry.grid(row=1, column=1, sticky="ew", pady=4)
        self.output_button = ttk.Button(paths, text="选择输出", command=self.choose_output)
        self.output_button.grid(row=1, column=2, padx=(8, 0))
        paths.columnconfigure(1, weight=1)
        filters = ttk.Frame(outer)
        filters.pack(fill="x", pady=(8, 3))
        for name, value in (("全部照片", "all"), ("仅 RAW", "raw"), ("仅 JPG", "jpg")):
            ttk.Radiobutton(filters, text=name, value=value, variable=self.selection).pack(side="left", padx=(0, 16))
        self.scan_button = ttk.Button(filters, text="扫描数量", command=self.start_scan)
        self.scan_button.pack(side="right")
        ttk.Label(outer, textvariable=self.scan_info, style="Sub.TLabel").pack(anchor="w", pady=(0, 10))
        presets = ttk.Frame(outer)
        presets.pack(fill="x")
        self.preset_buttons = []
        for name, value in (("原始 JPEG", "original"), ("微信朋友圈 · 2560px", "wechat"), ("高质量分享 · 4096px", "share")):
            button = ttk.Radiobutton(presets, text=name, value=value, variable=self.mode, command=self.set_preset)
            button.pack(side="left", padx=(0, 18), pady=8)
            self.preset_buttons.append(button)
        ttk.Label(outer, textvariable=self.mode_note, wraplength=880, style="Sub.TLabel").pack(anchor="w", pady=(0, 10))
        self.advanced = ttk.LabelFrame(outer, text="高级设置", padding=(12, 8))
        self.advanced.pack(fill="x")
        for column, (label, var, lo, hi) in enumerate((("长边 px", self.long_edge, 1, 30000),
              ("起始质量", self.quality, 88, 95), ("最低质量", self.minimum, 88, 95), ("正常上限 MB", self.cap, 0, 1000))):
            ttk.Label(self.advanced, text=label).grid(row=0, column=column * 2, sticky="w", padx=(0, 6))
            ttk.Spinbox(self.advanced, textvariable=var, from_=lo, to=hi, width=7).grid(row=0, column=column * 2 + 1, padx=(0, 14), pady=4)
        ttk.Checkbutton(self.advanced, text="轻度输出锐化（默认关闭）", variable=self.sharpen).grid(row=1, column=0, columnspan=3, sticky="w", pady=7)
        ttk.Checkbutton(self.advanced, text="保留拍摄 EXIF", variable=self.keep_exif).grid(row=1, column=3, columnspan=2, sticky="w")
        ttk.Checkbutton(self.advanced, text="保留目录结构", variable=self.structure).grid(row=1, column=5, columnspan=3, sticky="w")
        ttk.Label(self.advanced, text="分辨率不足").grid(row=2, column=0, sticky="w")
        ttk.Combobox(self.advanced, textvariable=self.below, values=("保留尺寸并标记", "跳过并标记"), state="readonly", width=18).grid(row=2, column=1, columnspan=3, sticky="w")
        ttk.Label(self.advanced, text="已有输出").grid(row=2, column=4, sticky="w")
        ttk.Combobox(self.advanced, textvariable=self.existing, values=("验证后断点续跑", "重新导出", "跳过已有文件"), state="readonly", width=18).grid(row=2, column=5, columnspan=3, sticky="w")
        ttk.Label(self.advanced, text="方向来源").grid(row=3, column=0, sticky="w", pady=7)
        ttk.Combobox(self.advanced, textvariable=self.orientation, values=("自动判断", "ARW / 文件外层", "JPEG 自身", "像素已正向"), state="readonly", width=18).grid(row=3, column=1, columnspan=3, sticky="w")
        ttk.Label(self.advanced, text="分享模式强制移除 GPS / XMP；1 MB = 1,000,000 字节", style="Sub.TLabel").grid(row=3, column=4, columnspan=4, sticky="w")
        action = ttk.Frame(outer)
        action.pack(fill="x", pady=(12, 6))
        self.pre_button = ttk.Button(action, text="快速预检", command=self.start_preflight)
        self.pre_button.pack(side="left")
        ttk.Checkbutton(action, text="先处理预检正常照片", variable=self.only_normal).pack(side="left", padx=12)
        self.start_button = ttk.Button(action, text="开始导出", style="Accent.TButton", command=self.start_export)
        self.start_button.pack(side="right")
        ttk.Label(outer, textvariable=self.preflight_info, wraplength=880, style="Sub.TLabel").pack(anchor="w", pady=(2, 10))
        progress = ttk.Frame(outer, style="Card.TFrame", padding=(14, 12))
        progress.pack(fill="both", expand=True)
        ttk.Label(progress, textvariable=self.counts, style="Card.TLabel").pack(anchor="w")
        self.bar = ttk.Progressbar(progress, mode="determinate")
        self.bar.pack(fill="x", pady=10)
        ttk.Label(progress, textvariable=self.current, style="Card.TLabel", wraplength=840).pack(anchor="w")
        ttk.Label(progress, textvariable=self.speed, style="Card.TLabel").pack(anchor="w", pady=(6, 2))
        ttk.Label(progress, textvariable=self.status, style="Card.TLabel", wraplength=840).pack(anchor="w", pady=(4, 6))
        bottom = ttk.Frame(outer)
        bottom.pack(fill="x", pady=(12, 0))
        self.pause_button = ttk.Button(bottom, text="暂停", state="disabled", command=self.pause)
        self.pause_button.pack(side="left")
        self.cancel_button = ttk.Button(bottom, text="安全取消", state="disabled", command=self.cancel)
        self.cancel_button.pack(side="left", padx=8)
        ttk.Button(bottom, text="查看错误详情", command=self.show_errors).pack(side="right")
        ttk.Button(bottom, text="打开输出文件夹", command=self.open_output).pack(side="right", padx=8)

    def set_preset(self):
        s = core.Settings.preset(self.mode.get())
        self.long_edge.set(str(s.long_edge))
        self.quality.set(str(s.quality))
        self.minimum.set(str(s.min_quality))
        self.cap.set(str(s.max_mb))
        self.sharpen.set(False)
        self.mode_note.set({"original": "原样保留全部 JPEG 字节：可能含 GPS；不保证方向、sRGB、分辨率或大小。高级画质设置不作用于原样模式。",
            "wechat": "最高 2560px，95→最低 88；小图保留尺寸；最低质量仍超 4MB 时保留画质并标记，不降低分辨率。",
            "share": "最高 4096px，质量 95；默认无大小限制；保留拍摄信息并移除定位元数据。"}[s.mode])
        self.precheck = None

    def choose_input(self):
        name = filedialog.askdirectory(title="选择照片文件夹")
        if name:
            self.set_input(name)

    def set_input(self, name):
        if self.busy:
            return
        self.input.set(name)
        if not self.output.get():
            path = Path(name)
            self.output.set(str(path.parent / (path.name + "_JPEG导出")))
        self.precheck = None
        self.start_scan()

    def choose_output(self):
        name = filedialog.askdirectory(title="选择输出根目录（必须在照片文件夹之外）")
        if name:
            self.output.set(name)

    def drop(self, event):
        names = self.root.tk.splitlist(event.data)
        if names and Path(names[0]).is_dir():
            self.set_input(names[0])
        else:
            messagebox.showinfo("拖入文件夹", "请拖入一个照片文件夹。")

    def settings(self):
        settings = core.Settings(mode=self.mode.get(), long_edge=int(self.long_edge.get()),
            quality=int(self.quality.get()), min_quality=int(self.minimum.get()), max_mb=float(self.cap.get()),
            sharpen=self.sharpen.get(), keep_exif=self.keep_exif.get(), keep_structure=self.structure.get(),
            existing={"验证后断点续跑": "resume", "重新导出": "replace", "跳过已有文件": "skip"}[self.existing.get()],
            below_policy="keep" if self.below.get() == "保留尺寸并标记" else "skip",
            orientation_source={"自动判断": "auto", "ARW / 文件外层": "raw", "JPEG 自身": "jpeg", "像素已正向": "none"}[self.orientation.get()])
        settings.validate()
        return settings

    def inputs(self):
        if not self.input.get().strip() or not self.output.get().strip():
            raise ValueError("请选择输入和输出文件夹")
        inp, out = Path(self.input.get()).resolve(), Path(self.output.get()).resolve()
        core.legacy.verify_paths(inp, out)
        return inp, out

    def launch(self, function):
        if self.busy:
            return
        self.busy = True
        self.control = core.Control()
        self.toggle_buttons()
        def worker():
            try:
                function()
            except core.Cancelled:
                self.events.put(("cancel_done", {}))
            except Exception as exc:
                self.events.put(("fatal", dict(error=str(exc), detail=traceback.format_exc())))
        threading.Thread(target=worker, daemon=True).start()

    def emit(self, event, data):
        self.events.put((event, data))

    def snapshot(self):
        # Only the GUI thread reads Tk variables; workers receive immutable snapshots.
        try:
            inp, out = self.inputs()
            s = self.settings()
            return inp, out, s, self.selection.get()
        except Exception as exc:
            messagebox.showerror("设置问题", str(exc))
            return None

    def start_scan(self):
        snapshot = self.snapshot()
        if not snapshot:
            return
        inp, out, s, selection = snapshot
        self.launch(lambda: self.emit("scanned", dict(zip(("photos", "stats"), core.scan(inp, out, selection, s.keep_structure)))))

    def start_preflight(self):
        snapshot = self.snapshot()
        if not snapshot:
            return
        inp, out, s, selection = snapshot
        def run():
            photos, stats = core.scan(inp, out, selection, s.keep_structure)
            self.emit("scan_stats", stats)
            result = core.preflight(photos, s, self.control, self.emit)
            result["snapshot"] = (str(inp), str(out), repr(s), selection)
            self.emit("precheck_saved", result)
        self.launch(run)

    def start_export(self):
        snapshot = self.snapshot()
        if not snapshot:
            return
        inp, out, s, selection = snapshot
        if self.only_normal.get():
            expected = (str(inp), str(out), repr(s), selection)
            if not self.precheck or self.precheck.get("snapshot") != expected or self.precheck.get("cancelled"):
                messagebox.showinfo("先预检", "请先按当前设置完成预检，再处理正常照片。")
                return
            problems = {x["file"] for x in self.precheck["problems"]}
        else:
            problems = set()
        self.errors = []
        def run():
            photos, stats = core.scan(inp, out, selection, s.keep_structure)
            self.emit("scan_stats", stats)
            photos = [p for p in photos if p.relative.as_posix() not in problems]
            core.batch(photos, inp, out, s, self.control, self.emit)
        self.launch(run)

    def toggle_buttons(self):
        for widget in (self.input_entry, self.output_entry, self.input_button, self.output_button,
                       self.scan_button, self.pre_button, self.start_button, *self.preset_buttons):
            widget.configure(state="disabled" if self.busy else "normal")
        self.pause_button.configure(state="normal" if self.busy else "disabled", text="暂停")
        self.cancel_button.configure(state="normal" if self.busy else "disabled")

    def pause(self):
        if self.control.paused.is_set():
            self.control.paused.clear()
            self.pause_button.configure(text="暂停")
            self.status.set("继续处理。")
        else:
            self.control.paused.set()
            self.pause_button.configure(text="继续")
            self.status.set("将在安全处理点暂停；当前工具调用可能需等待。")

    def cancel(self):
        self.control.cancelled.set()
        self.control.paused.clear()
        self.status.set("正在安全取消；已完成成果和进度会保留。")

    def poll(self):
        try:
            while True:
                event, data = self.events.get_nowait()
                if event in ("scan_stats", "scanned"):
                    stats = data["stats"] if event == "scanned" else data
                    self.scan_info.set(f"ARW {stats['arw']}　JPG/JPEG {stats['jpg']}　照片共 {stats['total']}　同名 RAW/JPG 配对 {stats['pairs']}")
                    if event == "scanned":
                        self.photos = data["photos"]
                        self.busy = False
                        self.toggle_buttons()
                elif event == "current":
                    self.current.set(data["file"])
                    self.status.set("正在提取和处理 JPEG…")
                elif event == "progress":
                    self.counts.set(f"总数 {data['total']}　已完成 {data['completed']}　成功 {data['success']}　跳过 {data['skipped']}　失败 {data['failed']}")
                    self.bar.configure(maximum=max(1, data["total"]), value=data["completed"])
                    self.speed.set(f"速度 {data['speed']:.1f} 张/分钟　预计剩余 {data['remaining_seconds']/60:.1f} 分钟")
                    if data["row"].get("status") == "error":
                        self.errors.append(f"{data['row']['relative_path']}: {data['row']['error']}")
                elif event == "retry":
                    self.status.set(f"文件占用，有限重试 {data['attempt']}/2：{data['file']}")
                elif event == "preflight_progress":
                    self.current.set(data["file"])
                    self.bar.configure(maximum=max(1, data["total"]), value=data["index"])
                    self.status.set(f"快速预检 {data['index']}/{data['total']}，尚未完整解码所有照片。")
                elif event == "preflight_done":
                    self.preflight_info.set(f"预检：初步正常 {data['provisional_ok']}；ARW 缺 JPEG {data['missing_jpeg']}；损坏/异常 {data['corrupt']}；低于目标 {data['resolution_below_target']}；可能超限 {data['possible_over_limit_estimate']}（估计）。")
                    self.errors = [f"{p['file']}: {p['error']}" for p in data["problems"]]
                elif event == "precheck_saved":
                    self.precheck = data
                    self.busy = False
                    self.toggle_buttons()
                    self.status.set("预检已取消。" if data["cancelled"] else "预检完成。正式导出时将验证完整 JPEG。")
                elif event in ("done", "cancel_done", "fatal"):
                    self.busy = False
                    self.toggle_buttons()
                    if event == "done":
                        self.last_output = data["output"]
                        self.status.set(f"{'已安全取消' if data['cancelled'] else '导出完成'}：成功 {data['success']}，跳过 {data['skipped']}，失败 {data['failed']}；低于目标 {data['resolution_below_target']}；超正常大小上限 {data['size_over_limit']}。")
                        self.speed.set("结果与清单已保存，可重新运行继续。")
                    elif event == "fatal":
                        self.errors.append(data["detail"])
                        self.status.set("任务停止：" + data["error"])
                        messagebox.showerror("处理问题", data["error"])
                    else:
                        self.status.set("已安全取消。")
        except queue.Empty:
            pass
        self.root.after(100, self.poll)

    def open_output(self):
        path = self.last_output or self.output.get()
        if path and Path(path).is_dir():
            os.startfile(path)
        else:
            messagebox.showinfo("输出目录", "输出目录尚未创建。")

    def show_errors(self):
        window = tk.Toplevel(self.root)
        window.title("错误与预检详情")
        window.geometry("850x420")
        text = tk.Text(window, wrap="word", font=("Microsoft YaHei UI", 10))
        text.pack(fill="both", expand=True, padx=12, pady=12)
        text.insert("end", "\n\n".join(self.errors) or "当前没有错误。")
        text.configure(state="disabled")

    def close(self):
        if self.busy:
            self.cancel()
            self.status.set("正在取消；任务停止后再次关闭窗口。")
        else:
            self.root.destroy()


def main():
    # Packaged diagnostic paths are offline and used by the build acceptance test.
    if "--self-test" in sys.argv:
        from bundled_tests import run_self_test
        target = Path(sys.argv[sys.argv.index("--self-test") + 1])
        return run_self_test(target)
    root = TkinterDnD.Tk()
    app = App(root)
    if "--gui-smoke" in sys.argv:
        target = Path(sys.argv[sys.argv.index("--gui-smoke") + 1])
        def smoke():
            target.write_text(json.dumps(dict(gui_started=True, title=root.title(),
                drag_drop=bool(root.tk.call("package", "present", "tkdnd")),
                presets=len(app.preset_buttons), buttons_ready=str(app.start_button.cget("state")) == "normal")), encoding="utf-8")
            root.destroy()
        root.after(1200, smoke)
    root.mainloop()
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        report = Path(os.environ.get("TEMP", ".")) / "WindowsJPGExporter_startup_error.txt"
        report.write_text(traceback.format_exc(), encoding="utf-8")
        try:
            messagebox.showerror("启动失败", f"启动失败，诊断保存在：\n{report}")
        except Exception:
            pass
        sys.exit(1)
