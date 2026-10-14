"""VidGrab 1.1 - تحميل الفيديوهات من TikTok / Instagram / Facebook / Twitter(X) / YouTube وغيرها.

مبنية فوق yt-dlp. للاستعمال الشخصي فقط، احترم حقوق أصحاب المحتوى وشروط المنصات.
"""

import io
import json
import os
import queue
import subprocess
import sys
import threading
import urllib.request
from datetime import datetime
from tkinter import filedialog, messagebox

import customtkinter as ctk
import yt_dlp
from PIL import Image

APP_NAME = "VidGrab"
APP_VERSION = "1.1.0"

# الاسم الظاهر -> (صيغة yt-dlp, صوت فقط؟)
QUALITIES = {
    "أفضل جودة": ("bv*+ba/b", False),
    "1080p": ("bv*[height<=1080]+ba/b[height<=1080]/b", False),
    "720p": ("bv*[height<=720]+ba/b[height<=720]/b", False),
    "480p": ("bv*[height<=480]+ba/b[height<=480]/b", False),
    "صوت فقط (MP3)": ("ba/b", True),
}

BROWSERS = ["بدون", "chrome", "edge", "firefox", "brave", "opera"]

THUMB_SIZE = (192, 108)
MAX_HISTORY = 200


# ---------------------------------------------------------------- أدوات مساعدة
class Cancelled(Exception):
    """يُرفع عند ضغط المستخدم على زر الإلغاء."""


class QueueLogger:
    """التطبيق بدون نافذة أوامر، فلا يوجد stdout/stderr: نحوّل الأخطاء لسجل الواجهة."""

    def __init__(self, q=None):
        self.q = q

    def debug(self, msg):
        pass

    def info(self, msg):
        pass

    def warning(self, msg):
        pass

    def error(self, msg):
        if self.q is not None:
            self.q.put(("log", str(msg)))


def resource_dir():
    return getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))


def ffmpeg_dir():
    """مجلد ffmpeg المدمج داخل exe، أو None للبحث في PATH."""
    base = resource_dir()
    if os.path.exists(os.path.join(base, "ffmpeg.exe")):
        return base
    return None


def data_dir():
    override = os.environ.get("VIDGRAB_HOME")
    if override:
        base = override
    else:
        base = os.environ.get("APPDATA") or os.path.join(os.path.expanduser("~"), ".config")
        base = os.path.join(base, APP_NAME)
    os.makedirs(base, exist_ok=True)
    return base


def load_json(name, default):
    try:
        with open(os.path.join(data_dir(), name), encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def save_json(name, data):
    try:
        with open(os.path.join(data_dir(), name), "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except OSError:
        pass


def default_folder():
    return os.path.join(os.path.expanduser("~"), "Downloads", APP_NAME)


def fmt_speed(speed):
    if not speed:
        return ""
    units = ["B/s", "KB/s", "MB/s", "GB/s"]
    i = 0
    while speed >= 1024 and i < len(units) - 1:
        speed /= 1024
        i += 1
    return f"{speed:.1f} {units[i]}"


def fmt_duration(seconds):
    if not seconds:
        return ""
    seconds = int(seconds)
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def open_path(path):
    if sys.platform.startswith("win"):
        os.startfile(path)  # noqa: S606
    elif sys.platform == "darwin":
        subprocess.Popen(["open", path])
    else:
        subprocess.Popen(["xdg-open", path])


def reveal_file(path):
    """يفتح المجلد مع تحديد الملف إن أمكن."""
    if sys.platform.startswith("win") and os.path.exists(path):
        subprocess.Popen(f'explorer /select,"{os.path.normpath(path)}"')
    else:
        open_path(os.path.dirname(path) or ".")


def looks_like_url(text):
    t = text.strip().lower()
    return t.startswith(("http://", "https://")) and " " not in t and "\n" not in t


# ---------------------------------------------------------------- العمليات الخلفية
def preview_worker(url, browser, token, q):
    opts = {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "skip_download": True,
        "logger": QueueLogger(None),
    }
    if browser != "بدون":
        opts["cookiesfrombrowser"] = (browser,)
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)
        is_list = info.get("_type") == "playlist"
        title = info.get("title") or "بدون عنوان"
        data = {
            "title": title,
            "duration": None if is_list else info.get("duration"),
            "uploader": info.get("uploader") or info.get("channel") or "",
            "playlist": is_list,
            "image": None,
        }
        thumb = info.get("thumbnail")
        if not thumb and info.get("thumbnails"):
            thumb = info["thumbnails"][-1].get("url")
        if thumb:
            try:
                req = urllib.request.Request(thumb, headers={"User-Agent": "Mozilla/5.0"})
                with urllib.request.urlopen(req, timeout=10) as r:  # noqa: S310
                    raw = r.read(5_000_000)
                img = Image.open(io.BytesIO(raw)).convert("RGB")
                img.thumbnail((THUMB_SIZE[0] * 2, THUMB_SIZE[1] * 2))
                data["image"] = img
            except Exception:  # noqa: BLE001
                pass
        q.put(("preview", token, data))
    except Exception as e:  # noqa: BLE001
        q.put(("preview_error", token, str(e).replace("ERROR: ", "")))


def download_worker(urls, cfg, q, cancel):
    fmt, audio_only = QUALITIES[cfg["quality"]]
    total = len(urls)
    ok = 0

    for idx, url in enumerate(urls, start=1):
        if cancel.is_set():
            break

        saved = []

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

        def pp_hook(d, url=url, saved=saved):
            if d["status"] == "finished" and d.get("postprocessor") == "MoveFiles":
                info = d.get("info_dict", {})
                path = info.get("filepath")
                if path:
                    saved.append(path)
                    q.put(("saved", info.get("title") or os.path.basename(path), path, url))

        if cfg["playlist"]:
            tmpl = os.path.join(cfg["outdir"], "%(playlist_title|Videos).60B", "%(title).70B [%(id)s].%(ext)s")
        else:
            tmpl = os.path.join(cfg["outdir"], "%(title).80B [%(id)s].%(ext)s")

        opts = {
            "format": fmt,
            "outtmpl": tmpl,
            "windowsfilenames": True,
            "noplaylist": not cfg["playlist"],
            "retries": 5,
            "fragment_retries": 5,
            "concurrent_fragment_downloads": 4,
            "quiet": True,
            "no_warnings": True,
            "noprogress": True,
            "logger": QueueLogger(q),
            "progress_hooks": [hook],
            "postprocessor_hooks": [pp_hook],
        }

        ff = ffmpeg_dir()
        if ff:
            opts["ffmpeg_location"] = ff

        pps = []
        if audio_only:
            pps.append({"key": "FFmpegExtractAudio", "preferredcodec": "mp3", "preferredquality": "192"})
        else:
            opts["merge_output_format"] = "mp4"
            if cfg["h264"]:
                opts["format_sort"] = ["vcodec:h264", "res", "acodec:m4a"]
            if cfg["subs"]:
                opts.update(
                    writesubtitles=True,
                    writeautomaticsub=True,
                    subtitleslangs=["ar", "en"],
                    subtitlesformat="srt/best",
                )
                pps.append({"key": "FFmpegSubtitlesConvertor", "format": "srt"})
        if pps:
            opts["postprocessors"] = pps

        if cfg["browser"] != "بدون":
            opts["cookiesfrombrowser"] = (cfg["browser"],)

        q.put(("status", f"({idx}/{total}) جاري البدء..."))
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                ydl.download([url])
            ok += 1
        except Cancelled:
            q.put(("log", "تم الإلغاء."))
            break
        except Exception as e:  # noqa: BLE001
            msg = str(e).replace("ERROR: ", "")
            q.put(("log", f"فشل: {url}\n   {msg}"))

    q.put(("done", ok, total, cancel.is_set()))


# ---------------------------------------------------------------- الواجهة
class App(ctk.CTk):
    def __init__(self):
        super().__init__()
        ctk.set_appearance_mode("dark")
        ctk.set_default_color_theme("blue")

        self.title(f"{APP_NAME} {APP_VERSION}")
        self.geometry("700x820")
        self.minsize(620, 720)

        self.font = "Segoe UI"
        self.q = queue.Queue()
        self.cancel = threading.Event()
        self.preview_token = 0
        self.last_auto_paste = ""
        self.running = False

        s = load_json("settings.json", {})
        self.folder = ctk.StringVar(value=s.get("folder") or default_folder())
        q_name = s.get("quality")
        self.quality = ctk.StringVar(value=q_name if q_name in QUALITIES else "أفضل جودة")
        b_name = s.get("browser")
        self.browser = ctk.StringVar(value=b_name if b_name in BROWSERS else "بدون")
        self.playlist = ctk.BooleanVar(value=bool(s.get("playlist", False)))
        self.subs = ctk.BooleanVar(value=bool(s.get("subs", False)))
        self.h264 = ctk.BooleanVar(value=bool(s.get("h264", True)))

        self.history = load_json("history.json", [])

        self._placeholder = ctk.CTkImage(
            Image.new("RGB", THUMB_SIZE, (45, 45, 48)), Image.new("RGB", THUMB_SIZE, (45, 45, 48)), THUMB_SIZE
        )

        self._build()
        self._render_history()

        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self.bind("<FocusIn>", self._on_focus)
        self.after(100, self._poll)
        self.after(400, self._auto_paste)

    # ---------- بناء الواجهة
    def F(self, size=13, weight="normal"):
        return ctk.CTkFont(family=self.font, size=size, weight=weight)

    def _build(self):
        head = ctk.CTkFrame(self, fg_color="transparent")
        head.pack(fill="x", padx=16, pady=(14, 4))
        ctk.CTkLabel(head, text=f"v{APP_VERSION}", font=self.F(12), text_color="gray").pack(side="left")
        ctk.CTkLabel(head, text=APP_NAME, font=self.F(24, "bold")).pack(side="right")

        self.tabs = ctk.CTkTabview(self)
        self.tabs.pack(fill="both", expand=True, padx=12, pady=(0, 6))
        self.tab_dl = self.tabs.add("التحميل")
        self.tab_hist = self.tabs.add("السجل")
        for t in self.tabs._segmented_button._buttons_dict.values():
            t.configure(font=self.F(13, "bold"))

        self._build_download_tab(self.tab_dl)
        self._build_history_tab(self.tab_hist)

        ctk.CTkLabel(
            self,
            text="للاستعمال الشخصي فقط. احترم حقوق أصحاب الفيديوهات وشروط المنصات.",
            font=self.F(11),
            text_color="gray",
        ).pack(pady=(0, 8))

    def _build_download_tab(self, tab):
        ctk.CTkLabel(tab, text="الصق الروابط (رابط في كل سطر)", font=self.F(13, "bold"), anchor="e").pack(
            fill="x", padx=6, pady=(4, 2)
        )
        self.links = ctk.CTkTextbox(tab, height=84, font=("Consolas", 12), wrap="none")
        self.links.pack(fill="x", padx=6)

        row = ctk.CTkFrame(tab, fg_color="transparent")
        row.pack(fill="x", padx=6, pady=6)
        ctk.CTkButton(row, text="لصق من الحافظة", width=120, font=self.F(), command=self._paste).pack(side="right")
        ctk.CTkButton(
            row, text="معاينة", width=90, font=self.F(), fg_color="transparent", border_width=1, command=self._preview
        ).pack(side="right", padx=6)
        ctk.CTkButton(
            row, text="مسح", width=70, font=self.F(), fg_color="transparent", border_width=1, command=self._clear
        ).pack(side="right")

        # المعاينة
        self.prev_frame = ctk.CTkFrame(tab, corner_radius=10)
        self.prev_frame.pack(fill="x", padx=6, pady=(0, 6))
        self.prev_img = ctk.CTkLabel(self.prev_frame, text="", image=self._placeholder, width=THUMB_SIZE[0])
        self.prev_img.pack(side="left", padx=10, pady=10)
        info = ctk.CTkFrame(self.prev_frame, fg_color="transparent")
        info.pack(side="right", fill="both", expand=True, padx=(0, 10), pady=10)
        self.prev_title = ctk.CTkLabel(
            info, text="المعاينة تظهر هنا", font=self.F(13, "bold"), anchor="e", justify="right", wraplength=360
        )
        self.prev_title.pack(fill="x")
        self.prev_meta = ctk.CTkLabel(info, text="", font=self.F(12), text_color="gray", anchor="e", justify="right")
        self.prev_meta.pack(fill="x", pady=(4, 0))

        # الخيارات
        opts = ctk.CTkFrame(tab, corner_radius=10)
        opts.pack(fill="x", padx=6, pady=(0, 6))
        opts.columnconfigure(0, weight=1)

        def line(r, label, widget):
            ctk.CTkLabel(opts, text=label, font=self.F(), anchor="e").grid(row=r, column=1, sticky="e", padx=10, pady=5)
            widget.grid(row=r, column=0, sticky="ew", padx=(10, 0), pady=5)

        line(0, "الجودة:", ctk.CTkOptionMenu(opts, variable=self.quality, values=list(QUALITIES), font=self.F()))
        line(1, "كوكيز المتصفح:", ctk.CTkOptionMenu(opts, variable=self.browser, values=BROWSERS, font=self.F()))

        sw = ctk.CTkFrame(opts, fg_color="transparent")
        sw.grid(row=2, column=0, columnspan=2, sticky="e", padx=10, pady=(2, 8))
        ctk.CTkSwitch(sw, text="قائمة تشغيل كاملة", variable=self.playlist, font=self.F(12)).pack(side="right", padx=8)
        ctk.CTkSwitch(sw, text="تحميل الترجمة", variable=self.subs, font=self.F(12)).pack(side="right", padx=8)
        ctk.CTkSwitch(sw, text="توافق أعلى (H.264)", variable=self.h264, font=self.F(12)).pack(side="right", padx=8)

        # المجلد
        frow = ctk.CTkFrame(tab, fg_color="transparent")
        frow.pack(fill="x", padx=6, pady=(0, 6))
        ctk.CTkButton(frow, text="تغيير...", width=80, font=self.F(), command=self._choose_folder).pack(side="left")
        ctk.CTkEntry(frow, textvariable=self.folder, font=self.F(12)).pack(side="left", fill="x", expand=True, padx=6)
        ctk.CTkLabel(frow, text="مجلد الحفظ:", font=self.F()).pack(side="right")

        # الأزرار
        btns = ctk.CTkFrame(tab, fg_color="transparent")
        btns.pack(fill="x", padx=6, pady=(0, 6))
        for c in range(3):
            btns.columnconfigure(c, weight=1)
        self.btn_start = ctk.CTkButton(btns, text="تحميل", height=38, font=self.F(14, "bold"), command=self._start)
        self.btn_start.grid(row=0, column=2, sticky="ew", padx=3)
        self.btn_cancel = ctk.CTkButton(
            btns, text="إلغاء", height=38, font=self.F(14), fg_color="#8a3b3b", hover_color="#a14747",
            state="disabled", command=self._cancel,
        )
        self.btn_cancel.grid(row=0, column=1, sticky="ew", padx=3)
        ctk.CTkButton(
            btns, text="فتح المجلد", height=38, font=self.F(14), fg_color="transparent", border_width=1,
            command=self._open_folder,
        ).grid(row=0, column=0, sticky="ew", padx=3)

        self.bar = ctk.CTkProgressBar(tab)
        self.bar.set(0)
        self.bar.pack(fill="x", padx=8, pady=(2, 2))
        self.status = ctk.CTkLabel(tab, text="جاهز", font=self.F(12), anchor="e")
        self.status.pack(fill="x", padx=8)

        self.log = ctk.CTkTextbox(tab, height=90, font=self.F(12), state="disabled")
        self.log.pack(fill="both", expand=True, padx=6, pady=(4, 4))

    def _build_history_tab(self, tab):
        top = ctk.CTkFrame(tab, fg_color="transparent")
        top.pack(fill="x", padx=6, pady=(4, 4))
        ctk.CTkButton(
            top, text="مسح السجل", width=100, font=self.F(12), fg_color="transparent", border_width=1,
            command=self._clear_history,
        ).pack(side="left")
        self.hist_count = ctk.CTkLabel(top, text="", font=self.F(12), text_color="gray")
        self.hist_count.pack(side="right")
        self.hist_list = ctk.CTkScrollableFrame(tab)
        self.hist_list.pack(fill="both", expand=True, padx=4, pady=4)

    # ---------- السجل
    def _render_history(self):
        for w in self.hist_list.winfo_children():
            w.destroy()
        items = self.history[:MAX_HISTORY]
        self.hist_count.configure(text=f"{len(items)} تحميل" if items else "")
        if not items:
            ctk.CTkLabel(
                self.hist_list, text="لا يوجد شيء بعد. فيديوهاتك المحمّلة تظهر هنا.", font=self.F(13), text_color="gray"
            ).pack(pady=40)
            return
        for it in items:
            exists = os.path.exists(it.get("path", ""))
            card = ctk.CTkFrame(self.hist_list, corner_radius=8)
            card.pack(fill="x", padx=2, pady=3)
            ctk.CTkLabel(
                card, text=it.get("title", ""), font=self.F(13, "bold"), anchor="e", justify="right", wraplength=520
            ).pack(fill="x", padx=10, pady=(8, 0))
            sub = it.get("date", "") + ("" if exists else "   (الملف محذوف أو منقول)")
            ctk.CTkLabel(card, text=sub, font=self.F(11), text_color="gray", anchor="e").pack(fill="x", padx=10)
            r = ctk.CTkFrame(card, fg_color="transparent")
            r.pack(fill="x", padx=8, pady=(4, 8))
            ctk.CTkButton(
                r, text="فتح الملف", width=90, height=28, font=self.F(12),
                state="normal" if exists else "disabled", command=lambda p=it["path"]: self._safe(open_path, p),
            ).pack(side="right", padx=3)
            ctk.CTkButton(
                r, text="إظهار في المجلد", width=110, height=28, font=self.F(12), fg_color="transparent",
                border_width=1, state="normal" if exists else "disabled",
                command=lambda p=it["path"]: self._safe(reveal_file, p),
            ).pack(side="right", padx=3)
            ctk.CTkButton(
                r, text="إعادة التحميل", width=110, height=28, font=self.F(12), fg_color="transparent",
                border_width=1, command=lambda u=it.get("url", ""): self._redownload(u),
            ).pack(side="right", padx=3)

    def _add_history(self, title, path, url):
        self.history = [h for h in self.history if h.get("path") != path]
        self.history.insert(
            0, {"title": title, "path": path, "url": url, "date": datetime.now().strftime("%Y-%m-%d  %H:%M")}
        )
        self.history = self.history[:MAX_HISTORY]
        save_json("history.json", self.history)
        self._render_history()

    def _clear_history(self):
        if self.history and messagebox.askyesno(APP_NAME, "مسح سجل التحميلات؟ (الملفات نفسها ما تنحذف)"):
            self.history = []
            save_json("history.json", [])
            self._render_history()

    def _redownload(self, url):
        if not url:
            return
        self.links.delete("1.0", "end")
        self.links.insert("1.0", url)
        self.tabs.set("التحميل")
        self._preview()

    def _safe(self, fn, *a):
        try:
            fn(*a)
        except Exception as e:  # noqa: BLE001
            messagebox.showerror(APP_NAME, f"تعذر فتح الملف:\n{e}")

    # ---------- الحافظة والمعاينة
    def _clipboard_url(self):
        try:
            text = self.clipboard_get().strip()
        except Exception:  # noqa: BLE001
            return ""
        return text if looks_like_url(text) else ""

    def _auto_paste(self):
        url = self._clipboard_url()
        current = self.links.get("1.0", "end").strip()
        if url and not current and url != self.last_auto_paste:
            self.last_auto_paste = url
            self.links.insert("1.0", url)
            self._preview()

    def _on_focus(self, event):
        if event.widget is self and not self.running:
            self.after(150, self._auto_paste)

    def _paste(self):
        url = self._clipboard_url()
        if url:
            self.links.insert("end", url + "\n")
            self._preview()
        else:
            self.status.configure(text="ما فيه رابط في الحافظة")

    def _clear(self):
        self.links.delete("1.0", "end")
        self._set_preview("المعاينة تظهر هنا", "", None)

    def _first_url(self):
        for line in self.links.get("1.0", "end").splitlines():
            if looks_like_url(line):
                return line.strip()
        return ""

    def _preview(self):
        url = self._first_url()
        if not url:
            return
        self.preview_token += 1
        self._set_preview("جاري جلب المعلومات...", url, None)
        threading.Thread(
            target=preview_worker, args=(url, self.browser.get(), self.preview_token, self.q), daemon=True
        ).start()

    def _set_preview(self, title, meta, pil_image):
        self.prev_title.configure(text=title)
        self.prev_meta.configure(text=meta)
        if pil_image is not None:
            img = ctk.CTkImage(pil_image, pil_image, THUMB_SIZE)
            self._thumb_ref = img
        else:
            img = self._placeholder
        self.prev_img.configure(image=img)

    # ---------- التحكم
    def _choose_folder(self):
        d = filedialog.askdirectory(initialdir=self.folder.get() or None)
        if d:
            self.folder.set(d)

    def _open_folder(self):
        path = self.folder.get()
        if os.path.isdir(path):
            self._safe(open_path, path)

    def _write_log(self, text):
        self.log.configure(state="normal")
        self.log.insert("end", text + "\n")
        self.log.see("end")
        self.log.configure(state="disabled")

    def _save_settings(self):
        save_json(
            "settings.json",
            {
                "folder": self.folder.get(),
                "quality": self.quality.get(),
                "browser": self.browser.get(),
                "playlist": self.playlist.get(),
                "subs": self.subs.get(),
                "h264": self.h264.get(),
            },
        )

    def _on_close(self):
        self._save_settings()
        self.cancel.set()
        self.destroy()

    def _start(self):
        urls = [u.strip() for u in self.links.get("1.0", "end").splitlines() if u.strip()]
        if not urls:
            messagebox.showwarning(APP_NAME, "الصق رابطاً واحداً على الأقل.")
            return
        bad = [u for u in urls if not looks_like_url(u)]
        if bad:
            messagebox.showwarning(APP_NAME, f"هذا ليس رابطاً صحيحاً:\n{bad[0]}")
            return

        outdir = self.folder.get().strip()
        try:
            os.makedirs(outdir, exist_ok=True)
        except OSError as e:
            messagebox.showerror(APP_NAME, f"تعذر إنشاء المجلد:\n{e}")
            return

        self._save_settings()
        cfg = {
            "outdir": outdir,
            "quality": self.quality.get(),
            "browser": self.browser.get(),
            "playlist": self.playlist.get(),
            "subs": self.subs.get(),
            "h264": self.h264.get(),
        }
        self.cancel.clear()
        self.running = True
        self.bar.set(0)
        self.btn_start.configure(state="disabled")
        self.btn_cancel.configure(state="normal")
        self.status.configure(text="جاري البدء...")
        threading.Thread(target=download_worker, args=(urls, cfg, self.q, self.cancel), daemon=True).start()

    def _cancel(self):
        self.cancel.set()
        self.status.configure(text="جاري الإلغاء...")

    def _poll(self):
        try:
            while True:
                msg = self.q.get_nowait()
                kind = msg[0]
                if kind == "progress":
                    self.bar.set(max(0.0, min(1.0, msg[1] / 100)))
                    self.status.configure(text=msg[2])
                elif kind == "status":
                    self.status.configure(text=msg[1])
                elif kind == "log":
                    self._write_log(msg[1])
                elif kind == "saved":
                    _, title, path, url = msg
                    self._add_history(title, path, url)
                    self._write_log(f"تم: {title}")
                elif kind == "preview":
                    _, token, data = msg
                    if token == self.preview_token:
                        meta = "  •  ".join(
                            x
                            for x in (
                                data["uploader"],
                                fmt_duration(data["duration"]),
                                "قائمة تشغيل" if data["playlist"] else "",
                            )
                            if x
                        )
                        self._set_preview(data["title"], meta, data["image"])
                elif kind == "preview_error":
                    _, token, err = msg
                    if token == self.preview_token:
                        self._set_preview("تعذرت المعاينة", err[:160], None)
                elif kind == "done":
                    _, ok, total, cancelled = msg
                    self.running = False
                    self.btn_start.configure(state="normal")
                    self.btn_cancel.configure(state="disabled")
                    if cancelled:
                        self.status.configure(text="تم الإلغاء")
                    else:
                        self.bar.set(1 if ok else 0)
                        self.status.configure(text=f"انتهى: نجح {ok} من {total}")
        except queue.Empty:
            pass
        self.after(100, self._poll)


def main():
    App().mainloop()


if __name__ == "__main__":
    main()
