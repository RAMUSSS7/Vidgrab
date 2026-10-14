"""VidGrab - أداة تحميل الفيديوهات من TikTok / Instagram / Facebook / Twitter(X) / YouTube وغيرها.

مبنية فوق yt-dlp. للاستعمال الشخصي فقط، احترم حقوق أصحاب المحتوى وشروط المنصات.
"""

import os
import queue
import sys
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

import yt_dlp

APP_TITLE = "VidGrab - تحميل الفيديوهات"

# الاسم الظاهر -> (صيغة yt-dlp, صوت فقط؟)
QUALITIES = {
    "أفضل جودة": ("bv*+ba/b", False),
    "1080p": ("bv*[height<=1080]+ba/b[height<=1080]/b", False),
    "720p": ("bv*[height<=720]+ba/b[height<=720]/b", False),
    "480p": ("bv*[height<=480]+ba/b[height<=480]/b", False),
    "صوت فقط (MP3)": ("ba/b", True),
}

BROWSERS = ["بدون", "chrome", "edge", "firefox", "brave", "opera"]


class Cancelled(Exception):
    """يُرفع عند ضغط المستخدم على زر الإلغاء."""


class QueueLogger:
    """مسجّل بسيط: التطبيق بدون نافذة أوامر، فلا يوجد stdout/stderr."""

    def __init__(self, q):
        self.q = q

    def debug(self, msg):
        pass

    def info(self, msg):
        pass

    def warning(self, msg):
        pass

    def error(self, msg):
        self.q.put(("log", str(msg)))


def resource_dir():
    return getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))


def ffmpeg_dir():
    """مجلد ffmpeg المدمج داخل exe، أو None للبحث في PATH."""
    base = resource_dir()
    if os.path.exists(os.path.join(base, "ffmpeg.exe")):
        return base
    return None


def default_folder():
    folder = os.path.join(os.path.expanduser("~"), "Downloads", "VidGrab")
    return folder


def fmt_speed(speed):
    if not speed:
        return ""
    units = ["B/s", "KB/s", "MB/s", "GB/s"]
    i = 0
    while speed >= 1024 and i < len(units) - 1:
        speed /= 1024
        i += 1
    return f"{speed:.1f} {units[i]}"


def download_worker(urls, outdir, quality, browser, q, cancel):
    fmt, audio_only = QUALITIES[quality]
    total = len(urls)
    ok = 0

    for idx, url in enumerate(urls, start=1):
        if cancel.is_set():
            break

        def hook(d, idx=idx):
            if cancel.is_set():
                raise Cancelled()
            if d["status"] == "downloading":
                size = d.get("total_bytes") or d.get("total_bytes_estimate")
                done = d.get("downloaded_bytes", 0)
                pct = (done / size * 100) if size else 0
                q.put(("progress", pct, f"({idx}/{total}) {pct:.0f}%  {fmt_speed(d.get('speed'))}"))
            elif d["status"] == "finished":
                q.put(("status", f"({idx}/{total}) جاري المعالجة..."))

        opts = {
            "format": fmt,
            "outtmpl": os.path.join(outdir, "%(title).80B [%(id)s].%(ext)s"),
            "windowsfilenames": True,
            "noplaylist": True,
            "retries": 5,
            "fragment_retries": 5,
            "quiet": True,
            "no_warnings": True,
            "noprogress": True,
            "logger": QueueLogger(q),
            "progress_hooks": [hook],
        }

        ff = ffmpeg_dir()
        if ff:
            opts["ffmpeg_location"] = ff

        if audio_only:
            opts["postprocessors"] = [
                {"key": "FFmpegExtractAudio", "preferredcodec": "mp3", "preferredquality": "192"}
            ]
        else:
            opts["merge_output_format"] = "mp4"

        if browser != "بدون":
            opts["cookiesfrombrowser"] = (browser,)

        q.put(("status", f"({idx}/{total}) جاري البدء..."))
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                ydl.download([url])
            ok += 1
            q.put(("log", f"تم: {url}"))
        except Cancelled:
            q.put(("log", "تم الإلغاء."))
            break
        except Exception as e:  # noqa: BLE001
            msg = str(e).replace("ERROR: ", "")
            q.put(("log", f"فشل: {url}\n   {msg}"))

    q.put(("done", ok, total, cancel.is_set()))


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title(APP_TITLE)
        self.geometry("640x560")
        self.minsize(560, 520)

        self.q = queue.Queue()
        self.cancel = threading.Event()
        self.worker = None

        self.folder = tk.StringVar(value=default_folder())
        self.quality = tk.StringVar(value="أفضل جودة")
        self.browser = tk.StringVar(value="بدون")

        self._build()
        self.after(100, self._poll)

    def _build(self):
        pad = {"padx": 12, "pady": 6}
        root = ttk.Frame(self)
        root.pack(fill="both", expand=True, padx=4, pady=4)
        root.columnconfigure(0, weight=1)

        ttk.Label(root, text="الصق الروابط (رابط في كل سطر):", anchor="e", font=("Segoe UI", 10, "bold")).grid(
            row=0, column=0, sticky="ew", **pad
        )

        links_frame = ttk.Frame(root)
        links_frame.grid(row=1, column=0, sticky="nsew", **pad)
        links_frame.columnconfigure(0, weight=1)
        self.links = tk.Text(links_frame, height=5, wrap="none", font=("Consolas", 10))
        self.links.grid(row=0, column=0, sticky="nsew")
        sb = ttk.Scrollbar(links_frame, command=self.links.yview)
        sb.grid(row=0, column=1, sticky="ns")
        self.links.configure(yscrollcommand=sb.set)

        ttk.Button(root, text="لصق من الحافظة", command=self._paste).grid(row=2, column=0, sticky="e", **pad)

        opts = ttk.Frame(root)
        opts.grid(row=3, column=0, sticky="ew", **pad)
        opts.columnconfigure(1, weight=1)

        ttk.Label(opts, text="الجودة:").grid(row=0, column=2, sticky="e", padx=4, pady=3)
        ttk.Combobox(opts, textvariable=self.quality, values=list(QUALITIES), state="readonly", justify="right").grid(
            row=0, column=1, sticky="ew", padx=4, pady=3
        )

        ttk.Label(opts, text="كوكيز المتصفح:").grid(row=1, column=2, sticky="e", padx=4, pady=3)
        ttk.Combobox(opts, textvariable=self.browser, values=BROWSERS, state="readonly", justify="right").grid(
            row=1, column=1, sticky="ew", padx=4, pady=3
        )
        ttk.Label(
            opts,
            text="(للفيديوهات التي تحتاج تسجيل دخول؛ أغلق المتصفح قبل التحميل)",
            foreground="#666",
            anchor="e",
        ).grid(row=2, column=0, columnspan=3, sticky="e", padx=4)

        ttk.Label(opts, text="مجلد الحفظ:").grid(row=3, column=2, sticky="e", padx=4, pady=3)
        ttk.Entry(opts, textvariable=self.folder, justify="left").grid(row=3, column=1, sticky="ew", padx=4, pady=3)
        ttk.Button(opts, text="تغيير...", command=self._choose_folder).grid(row=3, column=0, padx=4, pady=3)

        btns = ttk.Frame(root)
        btns.grid(row=4, column=0, sticky="ew", **pad)
        btns.columnconfigure(0, weight=1)
        btns.columnconfigure(1, weight=1)
        btns.columnconfigure(2, weight=1)
        self.btn_start = ttk.Button(btns, text="تحميل", command=self._start)
        self.btn_start.grid(row=0, column=2, sticky="ew", padx=3)
        self.btn_cancel = ttk.Button(btns, text="إلغاء", command=self._cancel, state="disabled")
        self.btn_cancel.grid(row=0, column=1, sticky="ew", padx=3)
        ttk.Button(btns, text="فتح المجلد", command=self._open_folder).grid(row=0, column=0, sticky="ew", padx=3)

        self.bar = ttk.Progressbar(root, maximum=100)
        self.bar.grid(row=5, column=0, sticky="ew", **pad)
        self.status = ttk.Label(root, text="جاهز", anchor="e")
        self.status.grid(row=6, column=0, sticky="ew", padx=12)

        self.log = tk.Text(root, height=8, state="disabled", wrap="word", font=("Segoe UI", 9))
        self.log.grid(row=7, column=0, sticky="nsew", **pad)
        root.rowconfigure(7, weight=1)

        ttk.Label(
            root,
            text="للاستعمال الشخصي فقط. احترم حقوق أصحاب الفيديوهات وشروط المنصات.",
            foreground="#888",
            anchor="e",
        ).grid(row=8, column=0, sticky="ew", padx=12, pady=(0, 6))

    # ---------- أحداث الواجهة ----------
    def _paste(self):
        try:
            text = self.clipboard_get().strip()
        except tk.TclError:
            return
        if text:
            self.links.insert("end", text + "\n")

    def _choose_folder(self):
        d = filedialog.askdirectory(initialdir=self.folder.get() or None)
        if d:
            self.folder.set(d)

    def _open_folder(self):
        path = self.folder.get()
        if os.path.isdir(path):
            if sys.platform.startswith("win"):
                os.startfile(path)  # noqa: S606
            elif sys.platform == "darwin":
                os.system(f'open "{path}"')
            else:
                os.system(f'xdg-open "{path}"')

    def _write_log(self, text):
        self.log.configure(state="normal")
        self.log.insert("end", text + "\n")
        self.log.see("end")
        self.log.configure(state="disabled")

    def _start(self):
        urls = [u.strip() for u in self.links.get("1.0", "end").splitlines() if u.strip()]
        if not urls:
            messagebox.showwarning(APP_TITLE, "الصق رابطاً واحداً على الأقل.")
            return
        bad = [u for u in urls if not u.lower().startswith(("http://", "https://"))]
        if bad:
            messagebox.showwarning(APP_TITLE, f"هذا ليس رابطاً صحيحاً:\n{bad[0]}")
            return

        outdir = self.folder.get().strip()
        try:
            os.makedirs(outdir, exist_ok=True)
        except OSError as e:
            messagebox.showerror(APP_TITLE, f"تعذر إنشاء المجلد:\n{e}")
            return

        self.cancel.clear()
        self.bar["value"] = 0
        self.btn_start.configure(state="disabled")
        self.btn_cancel.configure(state="normal")
        self.status.configure(text="جاري البدء...")
        self.worker = threading.Thread(
            target=download_worker,
            args=(urls, outdir, self.quality.get(), self.browser.get(), self.q, self.cancel),
            daemon=True,
        )
        self.worker.start()

    def _cancel(self):
        self.cancel.set()
        self.status.configure(text="جاري الإلغاء...")

    def _poll(self):
        try:
            while True:
                msg = self.q.get_nowait()
                kind = msg[0]
                if kind == "progress":
                    self.bar["value"] = msg[1]
                    self.status.configure(text=msg[2])
                elif kind == "status":
                    self.status.configure(text=msg[1])
                elif kind == "log":
                    self._write_log(msg[1])
                elif kind == "done":
                    _, ok, total, cancelled = msg
                    self.btn_start.configure(state="normal")
                    self.btn_cancel.configure(state="disabled")
                    if cancelled:
                        self.status.configure(text="تم الإلغاء")
                    else:
                        self.bar["value"] = 100 if ok else 0
                        self.status.configure(text=f"انتهى: نجح {ok} من {total}")
        except queue.Empty:
            pass
        self.after(100, self._poll)


def main():
    App().mainloop()


if __name__ == "__main__":
    main()
