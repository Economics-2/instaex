# -*- coding: utf-8 -*-
"""
Insta — লোকাল সার্ভার (Windows)
তৈরি করেছেন: MH & MIK

Android অ্যাপের LocalServer.kt-এর Python সংস্করণ। একই API endpoint রাখা হয়েছে,
তাই web/index.html প্রায় হুবহু একই থাকে।

yt-dlp ও ffmpeg আলাদা .exe হিসেবে (bin/ ফোল্ডারে) বান্ডেল করা থাকে এবং
subprocess দিয়ে চালানো হয়। এতে "yt-dlp আপডেট" বাটন exe-র ভেতরেও কাজ করে।
"""
import hmac
import json
import os
import re
import secrets
import shutil
import subprocess
import sys
import threading
import time
import uuid
import zipfile
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

APP_NAME = "Insta"
VERSION = "1.1.0"
IS_WIN = os.name == "nt"
PORT = 8765
MAX_PLAYLIST = 50
MAX_HISTORY = 200
CREATE_NO_WINDOW = 0x08000000

# শুধু এই ID-র Chrome এক্সটেনশন অ্যাপে লিংক পাঠাতে পারবে (extension/manifest.json-এর "key" থেকে নির্ধারিত)
EXT_IDS = {"nppejnkjfkkdpafpjmnodkelfdalegla"} | set(filter(None, os.environ.get("INSTA_EXT_ID", "").split(",")))

LANG_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{1,14}$")
ID_RE = re.compile(r"^[0-9a-f]{10}$")
ITEM_RE = re.compile(r"Downloading item (\d+) of (\d+)")
FMT_RE = re.compile(r"Downloading \d+ format\(s\): (\S+)")
SUB_EXTS = {"vtt", "srt", "ttml", "srv1", "srv2", "srv3", "json3", "ass", "lrc"}
PROGRESS_TEMPLATE = (
    "download:INSTA|%(progress.downloaded_bytes)s|%(progress.total_bytes)s|"
    "%(progress.total_bytes_estimate)s|%(progress.speed)s|%(progress.eta)s"
)


# ---------------------------------------------------------------- পাথ ও ফোল্ডার
def res(rel):
    """PyInstaller-এর ভেতরে ও বাইরে — দু'জায়গাতেই বান্ডেল করা ফাইলের পাথ।"""
    base = getattr(sys, "_MEIPASS", None) or os.path.dirname(os.path.abspath(__file__))
    return os.path.join(base, rel)


def data_dir():
    ov = os.environ.get("INSTA_DATA_DIR")
    if ov:
        p = Path(ov)
    elif IS_WIN:
        p = Path(os.environ.get("APPDATA") or Path.home() / "AppData" / "Roaming") / APP_NAME
    else:
        p = Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share") / APP_NAME
    p.mkdir(parents=True, exist_ok=True)
    return p


def downloads_dir():
    """ব্যবহারকারীর আসল Downloads ফোল্ডার (সরানো থাকলেও) + /Insta।"""
    ov = os.environ.get("INSTA_DOWNLOAD_DIR")
    base = Path(ov) if ov else None
    if base is None and IS_WIN:
        try:
            import ctypes

            class GUID(ctypes.Structure):
                _fields_ = [("a", ctypes.c_ulong), ("b", ctypes.c_ushort),
                            ("c", ctypes.c_ushort), ("d", ctypes.c_ubyte * 8)]

            # FOLDERID_Downloads
            guid = GUID(0x374DE290, 0x123F, 0x4565,
                        (ctypes.c_ubyte * 8)(0x91, 0x64, 0x39, 0xC4, 0x92, 0x5E, 0x46, 0x7B))
            ptr = ctypes.c_wchar_p()
            if ctypes.windll.shell32.SHGetKnownFolderPath(
                    ctypes.byref(guid), 0, None, ctypes.byref(ptr)) == 0:
                base = Path(ptr.value)
                ctypes.windll.ole32.CoTaskMemFree(ptr)
        except Exception:
            base = None
    if base is None:
        base = Path.home() / "Downloads"
    d = base if ov else base / APP_NAME
    d.mkdir(parents=True, exist_ok=True)
    return d


YTDLP_NAME = "yt-dlp.exe" if IS_WIN else "yt-dlp"


def bundled_ytdlp():
    return res(os.path.join("bin", YTDLP_NAME))


def user_ytdlp():
    return str(data_dir() / "bin" / YTDLP_NAME)


def ytdlp_path():
    """আপডেট-করা কপি থাকলে সেটা, নইলে বান্ডেল করা কপি।"""
    ov = os.environ.get("INSTA_YTDLP")
    if ov:
        return ov
    u = user_ytdlp()
    return u if os.path.exists(u) else bundled_ytdlp()


def ffmpeg_dir():
    return os.environ.get("INSTA_FFMPEG_DIR") or res("bin")


# ---------------------------------------------------------------- subprocess সহায়ক
_PROCS = set()
_PROCS_LOCK = threading.Lock()


def _popen(args, **kw):
    # stdin/stdout/stderr সবসময় স্পষ্ট দিতে হয়: --noconsole অ্যাপে এগুলো থাকে না।
    kw.setdefault("stdin", subprocess.DEVNULL)
    env = dict(os.environ, PYTHONUTF8="1", PYTHONIOENCODING="utf-8")
    return subprocess.Popen(args, env=env, creationflags=CREATE_NO_WINDOW if IS_WIN else 0, **kw)


def kill_tree(p):
    try:
        if IS_WIN:
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(p.pid)],
                           stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL, creationflags=CREATE_NO_WINDOW, timeout=10)
        else:
            p.kill()
    except Exception:
        try:
            p.kill()
        except Exception:
            pass


def run_capture(args, timeout):
    """চালিয়ে (returncode, stdout, stderr) ফেরত দেয়।"""
    p = _popen(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    with _PROCS_LOCK:
        _PROCS.add(p)
    try:
        out, err = p.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        kill_tree(p)
        p.communicate()
        raise
    finally:
        with _PROCS_LOCK:
            _PROCS.discard(p)
    return p.returncode, out.decode("utf-8", "replace"), err.decode("utf-8", "replace")


def kill_all():
    with _PROCS_LOCK:
        procs = list(_PROCS)
    for p in procs:
        kill_tree(p)


def tool_version(exe):
    try:
        rc, out, _ = run_capture([exe, "--version"], timeout=40)
        return out.strip().splitlines()[0] if rc == 0 and out.strip() else ""
    except Exception:
        return ""


# ---------------------------------------------------------------- প্রথমবার চালু
def init_binaries():
    """বান্ডেল করা yt-dlp.exe-র একটি কপি AppData-তে রাখে, যাতে আপডেট স্থায়ী হয়।"""
    if os.environ.get("INSTA_YTDLP"):
        return
    src = Path(bundled_ytdlp())
    if not src.exists():
        return
    dst = Path(user_ytdlp())
    dst.parent.mkdir(parents=True, exist_ok=True)
    if not dst.exists():
        shutil.copy2(src, dst)
        return

    def refresh():
        # নতুন Insta ভার্সনে নতুন yt-dlp থাকলে পুরোনো কপি বদলে দেয়।
        try:
            vb, vu = tool_version(str(src)), tool_version(str(dst))
            if vb and vu and vb > vu:
                tmp = dst.with_name(dst.name + ".new")
                shutil.copy2(src, tmp)
                os.replace(tmp, dst)
        except Exception:
            pass

    threading.Thread(target=refresh, daemon=True).start()


# ---------------------------------------------------------------- ছোট সহায়ক
class ApiError(Exception):
    def __init__(self, msg, status=400):
        super().__init__(msg)
        self.msg, self.status = msg, status


def valid_url(u):
    return bool(re.match(r"^https?://", u or "", re.I))


def sstr(o, key):
    v = o.get(key) if isinstance(o, dict) else None
    return v.strip() if isinstance(v, str) else ""


def fmt_eta(sec):
    if not sec or sec <= 0:
        return ""
    sec = int(sec)
    h, rem = divmod(sec, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


def fmt_speed(bps):
    if not bps or bps <= 0:
        return ""
    if bps >= 1048576:
        return f"{bps / 1048576:.1f} MB/s"
    return f"{bps / 1024:.0f} KB/s"


def num(s):
    try:
        return float(s)
    except (TypeError, ValueError):
        return None


def iso_now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------- তথ্য আনা
def api_info(url):
    if not valid_url(url):
        raise ApiError("সঠিক লিংক দিন (http বা https দিয়ে শুরু)।")

    # ভিডিও লিংকে list= থাকলে ডিফল্টভাবে শুধু ওই ভিডিও ধরা হয়
    video_in_list = "list=" in url and ("v=" in url or "youtu.be/" in url)
    args = [ytdlp_path(), "-J", "--flat-playlist", "--playlist-end", str(MAX_PLAYLIST),
            "--no-warnings", "--encoding", "utf-8"]
    if video_in_list:
        args.append("--no-playlist")
    args += ["--", url]

    try:
        rc, out, _ = run_capture(args, timeout=120)
        d = json.loads(out)
        if not isinstance(d, dict):
            raise ValueError("bad json")
    except Exception:
        raise ApiError("তথ্য আনা যায়নি। লিংকটি ঠিক আছে কিনা এবং কনটেন্ট পাবলিক কিনা দেখুন।")

    if d.get("_type") == "playlist":
        entries = d.get("entries") or []
        total = d.get("playlist_count") or len(entries)
        thumb = ""
        if entries and isinstance(entries[0], dict):
            th = entries[0].get("thumbnails") or []
            thumb = (th[-1].get("url") if th and isinstance(th[-1], dict) else None) \
                or entries[0].get("thumbnail") or ""
        return {
            "kind": "playlist",
            "title": d.get("title") or "প্লেলিস্ট",
            "uploader": d.get("uploader") or d.get("channel") or "",
            "count": total,
            "limit": min(total, MAX_PLAYLIST),
            "thumbnail": thumb,
            "heights": [1080, 720, 480, 360],
            "subs": [{"lang": l, "auto": None} for l in ("bn", "en", "hi")],
            "has_ffmpeg": True,
        }

    heights = set()
    for f in d.get("formats") or []:
        if not isinstance(f, dict):
            continue
        h = f.get("height")
        if isinstance(h, (int, float)) and h > 0 and (f.get("vcodec") or "") != "none":
            heights.add(int(h))

    manual = [k for k in (d.get("subtitles") or {}) if k != "live_chat"]
    auto = d.get("automatic_captions") or {}
    subs = [{"lang": k, "auto": False} for k in manual[:8]]
    for l in ("bn", "en", "hi", "ar", "ur"):
        if l in auto and l not in manual:
            subs.append({"lang": l, "auto": True})

    return {
        "kind": "video",
        "title": d.get("title") or "শিরোনাম নেই",
        "thumbnail": d.get("thumbnail") or "",
        "duration": int(d.get("duration") or 0),
        "uploader": d.get("uploader") or "",
        "heights": sorted(heights, reverse=True)[:6],
        "subs": subs,
        "also_playlist": video_in_list,
        "limit": MAX_PLAYLIST,
        "has_ffmpeg": True,
    }


# ---------------------------------------------------------------- ডাউনলোড
class Job:
    def __init__(self, kind, title="", source="app", url=""):
        self.kind = kind
        self.title = title
        self.source = source      # "app" = অ্যাপের নিজের UI, "ext" = ব্রাউজার এক্সটেনশন
        self.url = url
        self.created = time.time()
        self.dismissed = False
        self.status = "downloading"
        self.percent = 0.0
        self.speed = ""
        self.eta = ""
        self.index = 1
        self.total = 1
        self.error = ""
        self.streams = 1   # একটি ভিডিওর জন্য কয়টি স্ট্রিম (ভিডিও+অডিও = ২)
        self.stream = 0    # এই পর্যন্ত কয়টি স্ট্রিম শুরু হয়েছে
        self.in_sub = False

    def to_dict(self):
        return {"status": self.status, "percent": round(self.percent, 1), "speed": self.speed,
                "eta": self.eta, "index": self.index, "total": self.total, "error": self.error}


JOBS = {}
_ACTIVE = 0
_ACTIVE_LOCK = threading.Lock()
_HIST_LOCK = threading.Lock()


def _bump_active(n):
    global _ACTIVE
    with _ACTIVE_LOCK:
        _ACTIVE += n


def active_count():
    with _ACTIVE_LOCK:
        return _ACTIVE


def pick_format(kind, height):
    if kind == "audio":
        return "bestaudio/best"
    h = f"[height<={height}]" if height else ""
    return f"bestvideo{h}+bestaudio/best{h}/best"


def on_line(job, line):
    m = ITEM_RE.search(line)
    if m:
        job.index = int(m.group(1))
        job.total = min(int(m.group(2)), MAX_PLAYLIST)
        job.stream = 0
        return
    m = FMT_RE.search(line)
    if m:
        job.streams = max(1, m.group(1).count("+") + 1)
        return
    if line.startswith("[download] Destination:"):
        ext = line.rsplit(".", 1)[-1].strip().lower()
        job.in_sub = ext in SUB_EXTS
        if not job.in_sub:
            job.stream += 1
        return
    if not line.startswith("INSTA|") or job.in_sub:
        return
    p = line.split("|")
    if len(p) < 6:
        return
    done, total = num(p[1]), num(p[2]) or num(p[3])
    pct = max(0.0, min(100.0, done / total * 100)) if done is not None and total else 0.0
    job.speed = fmt_speed(num(p[4]))
    job.eta = fmt_eta(num(p[5]))
    within = (max(job.stream, 1) - 1 + pct / 100) / max(job.streams, 1)
    job.percent = ((job.index - 1) + min(within, 1.0)) / max(job.total, 1) * 100


def collect_output(tmp, title):
    """একটি ফাইল হলে সেটাই, একাধিক হলে (প্লেলিস্ট/সাবটাইটেল) জিপ করে ফেরত দেয়।"""
    files = sorted((f for f in tmp.iterdir()
                    if f.is_file() and f.suffix.lower() not in (".part", ".ytdl", ".temp")),
                   key=lambda f: f.name)
    if not files:
        return None
    if len(files) == 1:
        return files[0]
    safe = re.sub(r'[\\/:*?"<>|\x00-\x1f]+', "_", title).strip(" .")[:80] or "download"
    zp = tmp / f"{safe}.zip"
    with zipfile.ZipFile(zp, "w", zipfile.ZIP_STORED) as z:  # ভিডিও আগেই কমপ্রেসড
        for f in files:
            z.write(f, f.name)
    for f in files:
        f.unlink()
    return zp


def move_to_downloads(src):
    d = downloads_dir()
    dst = d / src.name
    n = 1
    while dst.exists():
        dst = d / f"{src.stem} ({n}){src.suffix}"
        n += 1
    shutil.move(str(src), str(dst))
    return dst


def write_error_log(lines):
    try:
        logs = data_dir() / "logs"
        logs.mkdir(exist_ok=True)
        (logs / "last-error.txt").write_text("\n".join(lines), encoding="utf-8")
    except Exception:
        pass


def run_download(job_id, url, kind, height, playlist, subs, title, cookies=None):
    job = JOBS[job_id]
    tmp = data_dir() / "tmp" / job_id
    tmp.mkdir(parents=True, exist_ok=True)
    _bump_active(1)
    proc = None
    tail = []
    cookie_file = None
    try:
        args = [ytdlp_path(), "--newline", "--no-colors", "--encoding", "utf-8",
                "--windows-filenames", "--no-mtime", "-N", "4",
                "--progress-template", PROGRESS_TEMPLATE]
        ff = ffmpeg_dir()
        if os.path.isdir(ff):
            args += ["--ffmpeg-location", ff]
        if cookies:
            cookie_file = write_cookie_file(job_id, cookies)
            args += ["--cookies", str(cookie_file)]
        if playlist:
            args += ["--yes-playlist", "--playlist-end", str(MAX_PLAYLIST),
                     "--ignore-errors"]  # একটি ভিডিও ব্যর্থ হলেও বাকিগুলো চলবে
        else:
            args += ["--no-playlist"]

        args += ["-f", pick_format(kind, height)]
        if kind == "audio":
            args += ["-x", "--audio-format", "mp3", "--audio-quality", "192K"]
        else:
            args += ["-S", "ext:mp4:m4a", "--merge-output-format", "mp4"]

        if subs:
            if any(a is not True for _, a in subs):
                args.append("--write-subs")
            if any(a is not False for _, a in subs):
                args.append("--write-auto-subs")
            args += ["--sub-langs", ",".join(l for l, _ in subs),
                     "--sub-format", "srt/vtt/best", "--convert-subs", "srt"]

        template = ("%(playlist_index)03d_%(title).80s.%(ext)s" if playlist
                    else "%(title).80s.%(ext)s")
        args += ["-o", str(tmp / template), "--", url]

        proc = _popen(args, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                      text=True, encoding="utf-8", errors="replace", bufsize=1)
        with _PROCS_LOCK:
            _PROCS.add(proc)
        for raw in proc.stdout:
            line = raw.rstrip("\r\n")
            tail.append(line)
            del tail[:-60]
            on_line(job, line)
        rc = proc.wait()

        has_files = any(f.is_file() and f.suffix.lower() not in (".part", ".ytdl", ".temp")
                        for f in tmp.iterdir())
        # প্লেলিস্টে কিছু ভিডিও নামলে সেগুলো রাখা হয়, কিছুই না নামলে ব্যর্থ ধরা হয়
        if rc != 0 and (not playlist or not has_files):
            raise RuntimeError(f"yt-dlp exit code {rc}")

        job.status = "processing"
        job.percent = 100.0
        out = collect_output(tmp, title)
        if out is None:
            raise RuntimeError("no output files")
        final = move_to_downloads(out)
        if job.source == "ext" and final.suffix.lower() != ".zip":
            title = final.stem   # এক্সটেনশন থেকে এলে আসল শিরোনাম ফাইলের নাম থেকে
        job.title = title

        add_history({
            "id": job_id, "title": title, "url": url,
            "kind": "playlist" if playlist else kind,
            "quality": "audio" if kind == "audio" else (f"{height}p" if height else "best"),
            "subs": [l for l, _ in subs], "name": final.name, "path": str(final),
            "size": final.stat().st_size, "time": iso_now(),
        })
        job.status = "done"
        if IS_WIN:
            try:
                import winsound
                winsound.MessageBeep()
            except Exception:
                pass
    except Exception as e:
        job.status = "error"
        job.error = "ডাউনলোড ব্যর্থ হয়েছে। অন্য কোয়ালিটি বেছে আবার চেষ্টা করুন।"
        write_error_log([f"url: {url}", f"error: {e!r}", "--- yt-dlp output (শেষ অংশ) ---"] + tail)
    finally:
        if proc is not None:
            with _PROCS_LOCK:
                _PROCS.discard(proc)
        shutil.rmtree(tmp, ignore_errors=True)
        if cookie_file is not None:
            try:
                cookie_file.unlink()
            except OSError:
                pass
        _bump_active(-1)


def start_job(url, kind, height, playlist, subs, title, source="app", cookies=None):
    job_id = uuid.uuid4().hex[:10]
    JOBS[job_id] = Job(kind, title, source, url)
    threading.Thread(target=run_download, daemon=True,
                     args=(job_id, url, kind, height, playlist, subs, title, cookies)).start()
    return job_id


def api_download(b):
    url = sstr(b, "url")
    if not valid_url(url):
        raise ApiError("সঠিক লিংক দিন।")
    kind = "audio" if sstr(b, "kind") == "audio" else "video"
    h = b.get("height")
    height = int(h) if (isinstance(h, (str, int)) and str(h).isdigit() and 0 < int(h) < 10000) else None
    playlist = bool(b.get("playlist"))
    title = sstr(b, "title") or "download"

    subs = []
    arr = b.get("subs") if isinstance(b.get("subs"), list) else []
    for o in arr[:10]:
        if not isinstance(o, dict):
            continue
        lang = sstr(o, "lang")
        if not LANG_RE.match(lang):
            continue
        a = o.get("auto")
        subs.append((lang, a if isinstance(a, bool) else None))

    return {"job": start_job(url, kind, height, playlist, subs, title)}


def api_status(job_id):
    job = JOBS.get(job_id)
    if job is None:
        raise ApiError("কাজটি পাওয়া যায়নি।", 404)
    return job.to_dict()


# ---------------------------------------------------------------- ব্রাউজার এক্সটেনশন
def clean_cookies(raw):
    """এক্সটেনশন থেকে আসা কুকি যাচাই করে (ঐচ্ছিক, লগইন-লাগা ভিডিওর জন্য)।"""
    out = []
    if not isinstance(raw, list):
        return out
    bad = ("\t", "\r", "\n", "\x00")
    for c in raw[:400]:
        if not isinstance(c, dict):
            continue
        name, value, domain, path = (c.get("name"), c.get("value"), c.get("domain"), c.get("path"))
        if not all(isinstance(x, str) for x in (name, value, domain, path)):
            continue
        if not name or not re.match(r"^\.?[A-Za-z0-9.-]+$", domain) or not path.startswith("/"):
            continue
        if any(ch in x for x in (name, value, path) for ch in bad):
            continue
        exp = c.get("expirationDate")
        out.append((domain, path, bool(c.get("secure")), bool(c.get("httpOnly")),
                    int(exp) if isinstance(exp, (int, float)) and exp > 0 else 0, name, value))
    return out


def write_cookie_file(job_id, cookies):
    p = data_dir() / "tmp" / f"{job_id}.cookies.txt"
    p.parent.mkdir(parents=True, exist_ok=True)
    lines = ["# Netscape HTTP Cookie File"]
    for domain, path, secure, httponly, exp, name, value in cookies:
        lines.append("\t".join([("#HttpOnly_" if httponly else "") + domain,
                                "TRUE" if domain.startswith(".") else "FALSE", path,
                                "TRUE" if secure else "FALSE", str(exp), name, value]))
    p.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return p


def api_ext_send(b):
    """এক্সটেনশন থেকে লিংক এলে সাথে সাথে সেরা কোয়ালিটিতে ডাউনলোড শুরু।"""
    url = sstr(b, "url")
    if not valid_url(url) or len(url) > 2000:
        raise ApiError("সঠিক লিংক নয়।")
    for jid, j in list(JOBS.items()):   # একই লিংকে দু'বার চাপলে নতুন কাজ হবে না
        if (j.source == "ext" and j.url == url and not j.dismissed
                and j.status in ("downloading", "processing")):
            return {"ok": True, "job": jid, "duplicate": True}
    title = sstr(b, "title")[:150] or "Insta ডাউনলোড"
    cookies = clean_cookies(b.get("cookies"))
    return {"ok": True, "job": start_job(url, "video", None, False, [], title, "ext", cookies)}


def api_jobs():
    items = [(j.created, dict(j.to_dict(), id=jid, title=j.title))
             for jid, j in list(JOBS.items()) if j.source == "ext" and not j.dismissed]
    items.sort(key=lambda x: -x[0])
    return [d for _, d in items[:10]]


def dismiss_job(job_id):
    j = JOBS.get(job_id)
    if j is not None:
        j.dismissed = True


def api_extension():
    """বান্ডেল করা এক্সটেনশন AppData-তে কপি করে ফোল্ডার খোলে।"""
    src = Path(res("extension"))
    if not src.is_dir():
        raise ApiError("এক্সটেনশন ফোল্ডার পাওয়া যায়নি।", 404)
    dst = data_dir() / "extension"
    shutil.copytree(src, dst, dirs_exist_ok=True)
    try:
        if IS_WIN:
            os.startfile(str(dst))  # noqa
        else:
            _popen(["xdg-open", str(dst)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception:
        pass
    return {"path": str(dst)}


def ext_origin_ok(origin):
    pre = "chrome-extension://"
    return origin.startswith(pre) and origin[len(pre):].rstrip("/") in EXT_IDS


# ---------------------------------------------------------------- yt-dlp আপডেট
def api_update():
    if active_count() > 0:
        raise ApiError("ডাউনলোড চলাকালীন আপডেট করা যাবে না। শেষ হলে আবার চেষ্টা করুন।")
    exe = ytdlp_path()
    before = tool_version(exe)
    try:
        rc, out, _ = run_capture([exe, "-U", "--encoding", "utf-8"], timeout=300)
    except Exception:
        raise ApiError("আপডেট করা যায়নি। ইন্টারনেট সংযোগ দেখুন।")
    after = tool_version(exe)
    if rc != 0 and after == before:
        raise ApiError("আপডেট করা যায়নি। ইন্টারনেট সংযোগ দেখুন।")
    changed = bool(after) and after != before
    return {"status": "DONE" if changed or "Updated yt-dlp" in out else "ALREADY_UP_TO_DATE",
            "version": after or before}


# ---------------------------------------------------------------- ইতিহাস
def _hist_file():
    return data_dir() / "history.json"


def _read_history():
    try:
        d = json.loads(_hist_file().read_text(encoding="utf-8"))
        return d if isinstance(d, list) else []
    except Exception:
        return []


def _write_history(items):
    f = _hist_file()
    tmp = f.with_name(f.name + ".tmp")
    tmp.write_text(json.dumps(items, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, f)


def api_history():
    with _HIST_LOCK:
        items = _read_history()
    for it in items:
        p = it.get("path")
        it["exists"] = bool(p) and os.path.exists(p)
    return items


def add_history(item):
    with _HIST_LOCK:
        _write_history(([item] + _read_history())[:MAX_HISTORY])


def remove_history(item_id):
    with _HIST_LOCK:
        if item_id is None:
            _write_history([])
        else:
            _write_history([x for x in _read_history() if x.get("id") != item_id])


def api_reveal(b):
    item_id = sstr(b, "id")
    if not ID_RE.match(item_id):
        raise ApiError("ভুল আইডি।")
    with _HIST_LOCK:
        item = next((x for x in _read_history() if x.get("id") == item_id), None)
    path = (item or {}).get("path")
    if not path:
        raise ApiError("ফাইলটি পাওয়া যায়নি।", 404)
    # শুধু Insta-র নিজের ডাউনলোড ফোল্ডারের ভেতরের ফাইল দেখানো হয়
    root = os.path.realpath(str(downloads_dir()))
    if not os.path.realpath(path).startswith(root):
        raise ApiError("ভুল পাথ।")
    try:
        if IS_WIN:
            if os.path.exists(path):
                subprocess.Popen(f'explorer /select,"{path}"', creationflags=CREATE_NO_WINDOW,
                                 stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                 stderr=subprocess.DEVNULL)
            else:
                os.startfile(root)  # noqa
        else:
            _popen(["xdg-open", root], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception:
        raise ApiError("ফোল্ডার খোলা যায়নি।")
    return {"ok": True}


# ---------------------------------------------------------------- HTTP
class Handler(BaseHTTPRequestHandler):
    server_version = "Insta"
    token = ""
    _cors = None

    def log_message(self, *a):   # --noconsole-এ stderr নেই, তাই লগ বন্ধ
        pass

    def _send(self, code, body, ctype):
        try:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            if self._cors:
                self.send_header("Access-Control-Allow-Origin", self._cors)
                self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
                self.send_header("Access-Control-Allow-Headers", "Content-Type")
                self.send_header("Access-Control-Max-Age", "600")
                self.send_header("Vary", "Origin")
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            pass

    def _json(self, obj, code=200):
        self._send(code, json.dumps(obj, ensure_ascii=False).encode("utf-8"),
                   "application/json; charset=utf-8")

    def _file(self, rel, ctype):
        try:
            with open(res(rel), "rb") as f:
                self._send(200, f.read(), ctype)
        except OSError:
            self._json({"error": "পাওয়া যায়নি।"}, 404)

    def _body(self):
        try:
            n = min(int(self.headers.get("Content-Length") or 0), 1 << 20)
            d = json.loads(self.rfile.read(n).decode("utf-8")) if n else {}
            return d if isinstance(d, dict) else {}
        except Exception:
            return {}

    def _route(self, method):
        path = urlparse(self.path).path
        try:
            if method == "GET" and path in ("/", "/index.html"):
                return self._file("web/index.html", "text/html; charset=utf-8")
            if method == "GET" and path in ("/logo.png", "/favicon.ico"):
                return self._file("web/logo.png", "image/png")
            if path.startswith("/ext/"):
                origin = self.headers.get("Origin") or ""
                if not ext_origin_ok(origin):
                    return self._json({"error": "অনুমতি নেই।"}, 403)
                self._cors = origin
                if method == "OPTIONS":
                    return self._send(204, b"", "text/plain")
                if method in ("GET", "POST") and path == "/ext/ping":
                    return self._json({"ok": True, "app": APP_NAME, "version": VERSION})
                if method == "POST" and path == "/ext/send":
                    return self._json(api_ext_send(self._body()))
                return self._json({"error": "পাওয়া যায়নি।"}, 404)
            if not path.startswith("/api/"):
                return self._json({"error": "পাওয়া যায়নি।"}, 404)
            if not hmac.compare_digest(self.headers.get("X-Token") or "", self.token):
                return self._json({"error": "অনুমতি নেই।"}, 403)

            if method == "POST" and path == "/api/info":
                return self._json(api_info(sstr(self._body(), "url")))
            if method == "POST" and path == "/api/download":
                return self._json(api_download(self._body()))
            if method == "POST" and path == "/api/update":
                return self._json(api_update())
            if method == "POST" and path == "/api/extension":
                return self._json(api_extension())
            if method == "GET" and path == "/api/jobs":
                return self._json(api_jobs())
            if method == "DELETE" and path.startswith("/api/jobs/"):
                jid = path[len("/api/jobs/"):]
                if not ID_RE.match(jid):
                    raise ApiError("ভুল আইডি।")
                dismiss_job(jid)
                return self._json({"ok": True})
            if method == "POST" and path == "/api/reveal":
                return self._json(api_reveal(self._body()))
            if method == "GET" and path.startswith("/api/status/"):
                return self._json(api_status(path[len("/api/status/"):]))
            if method == "GET" and path == "/api/history":
                return self._json(api_history())
            if method == "DELETE" and path == "/api/history":
                remove_history(None)
                return self._json({"ok": True})
            if method == "DELETE" and path.startswith("/api/history/"):
                hid = path[len("/api/history/"):]
                if not ID_RE.match(hid):
                    raise ApiError("ভুল আইডি।")
                remove_history(hid)
                return self._json({"ok": True})
            return self._json({"error": "পাওয়া যায়নি।"}, 404)
        except ApiError as e:
            self._json({"error": e.msg}, e.status)
        except Exception:
            self._json({"error": "সার্ভারে সমস্যা হয়েছে।"}, 500)

    def do_GET(self):
        self._route("GET")

    def do_POST(self):
        self._route("POST")

    def do_DELETE(self):
        self._route("DELETE")

    def do_OPTIONS(self):
        self._route("OPTIONS")


class InstaServer:
    def __init__(self):
        self.token = secrets.token_hex(16)
        Handler.token = self.token
        try:
            self.httpd = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
        except OSError:   # ৮৭৬৫ ব্যস্ত থাকলে যেকোনো ফাঁকা পোর্ট
            self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.httpd.daemon_threads = True
        self.port = self.httpd.server_address[1]
        self.url = f"http://127.0.0.1:{self.port}/?t={self.token}"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def stop(self):
        kill_all()
        try:
            self.httpd.shutdown()
        except Exception:
            pass
        shutil.rmtree(data_dir() / "tmp", ignore_errors=True)


def start():
    shutil.rmtree(data_dir() / "tmp", ignore_errors=True)   # আগের বারের অসমাপ্ত ফাইল
    init_binaries()
    return InstaServer()
