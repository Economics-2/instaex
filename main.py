# -*- coding: utf-8 -*-
"""
Insta — ভিডিও ডাউনলোডার (Windows)
তৈরি করেছেন: MH & MIK

চালু করলে লোকাল সার্ভার শুরু হয় এবং একটি নেটিভ উইন্ডোতে (Edge WebView2) UI খোলে।
"""
import os
import sys
import urllib.error
import urllib.request

import server

IS_WIN = os.name == "nt"
WEBVIEW2_URL = "https://developer.microsoft.com/microsoft-edge/webview2/"


def message_box(text, title="Insta", yes_no=False):
    """Windows মেসেজ বক্স। yes_no হলে 'হ্যাঁ' চাপলে True।"""
    if not IS_WIN:
        return False
    import ctypes
    flags = 0x24 if yes_no else 0x40   # MB_YESNO|MB_ICONQUESTION  /  MB_ICONINFORMATION
    return ctypes.windll.user32.MessageBoxW(0, text, title, flags) == 6


def webview2_installed():
    if not IS_WIN:
        return True
    import winreg
    guid = r"SOFTWARE\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}"
    places = [(winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node" + guid[8:]),
              (winreg.HKEY_LOCAL_MACHINE, guid),
              (winreg.HKEY_CURRENT_USER, guid)]
    for root, sub in places:
        try:
            with winreg.OpenKey(root, sub) as k:
                v, _ = winreg.QueryValueEx(k, "pv")
                if v and v != "0.0.0.0":
                    return True
        except OSError:
            continue
    return False


# ---------------------------------------------------------------- সেলফটেস্ট (CI-তে ব্যবহৃত)
def selftest(outfile):
    lines, ok = [], True

    def check(name, fn):
        nonlocal ok
        try:
            lines.append(f"PASS {name}: {fn()}")
        except Exception as e:
            ok = False
            lines.append(f"FAIL {name}: {e!r}")

    def http(path, method="GET", token=None, body=None):
        req = urllib.request.Request(f"http://127.0.0.1:{srv.port}{path}", method=method,
                                     data=body, headers={"Content-Type": "application/json"})
        if token:
            req.add_header("X-Token", token)
        try:
            with urllib.request.urlopen(req, timeout=20) as r:
                return r.status, r.read()
        except urllib.error.HTTPError as e:
            return e.code, e.read()

    srv = server.start()
    try:
        if IS_WIN:
            check("import webview", lambda: __import__("webview").__name__)
            check("import clr", lambda: __import__("clr").__name__)
        check("bundled index.html", lambda: os.path.getsize(server.res("web/index.html")))
        check("bundled logo", lambda: os.path.getsize(server.res("web/logo.png")))
        check("bundled extension", lambda: os.path.getsize(server.res("extension/manifest.json")))
        check("GET /", lambda: "Insta" in http("/")[1].decode("utf-8") or (_ for _ in ()).throw(
            AssertionError("page has no 'Insta'")))
        check("api without token -> 403", lambda: http("/api/history")[0] == 403 or (_ for _ in ()).throw(
            AssertionError("expected 403")))
        check("api history", lambda: http("/api/history", token=srv.token)[1][:40])
        check("api info bad url -> 400", lambda: http(
            "/api/info", "POST", srv.token, b'{"url":"abc"}')[0] == 400 or (_ for _ in ()).throw(
            AssertionError("expected 400")))
        check("yt-dlp --version", lambda: server.tool_version(server.ytdlp_path()) or (_ for _ in ()).throw(
            AssertionError("no version")))

        def ffmpeg():
            exe = os.path.join(server.ffmpeg_dir(), "ffmpeg.exe" if IS_WIN else "ffmpeg")
            rc, out, _ = server.run_capture([exe, "-version"], timeout=60)
            if rc != 0:
                raise AssertionError(f"exit {rc}")
            return out.splitlines()[0]

        check("ffmpeg -version", ffmpeg)
    finally:
        srv.stop()
    with open(outfile, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    return 0 if ok else 1


# ---------------------------------------------------------------- প্রধান
def main():
    if "--selftest" in sys.argv:
        i = sys.argv.index("--selftest")
        out = sys.argv[i + 1] if len(sys.argv) > i + 1 else "selftest.txt"
        os._exit(selftest(out))

    if IS_WIN and not webview2_installed():
        if message_box("Insta চালাতে Microsoft Edge WebView2 Runtime দরকার, কিন্তু এটি পাওয়া যায়নি।\n\n"
                       "ডাউনলোড পেজ খুলব? (Evergreen Bootstrapper ইনস্টল করে আবার চালু করুন)",
                       yes_no=True):
            os.startfile(WEBVIEW2_URL)   # noqa
            os._exit(0)

    srv = server.start()
    try:
        import webview
        webview.create_window("Insta", srv.url, width=580, height=880, min_size=(380, 620),
                              background_color="#EDF3EE")
        icon = server.res(os.path.join("assets", "icon.ico"))
        webview.start(gui="edgechromium" if IS_WIN else None,
                      icon=icon if os.path.exists(icon) else None)
    except Exception as e:
        message_box(f"Insta চালু করা যায়নি।\n\n{e}", "Insta — সমস্যা")
    finally:
        srv.stop()
        os._exit(0)   # ব্যাকগ্রাউন্ড থ্রেড/yt-dlp প্রসেস থাকলেও পুরোপুরি বন্ধ


if __name__ == "__main__":
    main()
