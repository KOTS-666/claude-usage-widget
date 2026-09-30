"""Claude usage widget: 5-hour and weekly limit usage on the desktop.

Reads the OAuth token that Claude Code stores in ~/.claude/.credentials.json
and polls the usage endpoint. It sends no model requests, so it spends no tokens.
Stdlib only: run with pythonw widget.pyw
"""
import ctypes
import ctypes.wintypes as wt
import json
import os
import queue
import sys
import threading
import time
import tkinter as tk
import urllib.error
import urllib.request
from datetime import datetime, timezone

CREDS = os.path.join(os.path.expanduser("~"), ".claude", ".credentials.json")
FROZEN = getattr(sys, "frozen", False)  # built into an .exe by PyInstaller
if FROZEN:  # the .exe unpacks to a temp dir, keep settings in the user profile
    _state_dir = os.path.join(os.environ.get("LOCALAPPDATA", ""), "ClaudeUsageWidget")
    os.makedirs(_state_dir, exist_ok=True)
    STATE = os.path.join(_state_dir, "state.json")
else:
    STATE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "state.json")
STARTUP_VBS = os.path.join(
    os.environ.get("APPDATA", ""),
    r"Microsoft\Windows\Start Menu\Programs\Startup\claude_usage_widget.vbs",
)
URL = "https://api.anthropic.com/api/oauth/usage"
POLL_MS = 3 * 60 * 1000
BACKOFF_MIN = (5, 10, 20, 30)  # minutes to wait after consecutive HTTP 429

BG, FG, DIM, TRACK = "#1e1f24", "#e8e8ea", "#8a8c94", "#33353c"
BASE_H = 40  # design height of the widget; scales with the real taskbar
SEC_W = 161  # width of one section at BASE_H

user32 = ctypes.WinDLL("user32")
# 64-bit handles: without argtypes a handle or HWND_TOPMOST (-1) is passed as a
# 32-bit int and calls sometimes fail with "invalid window handle"
user32.SetWindowPos.argtypes = (wt.HWND, wt.HWND, ctypes.c_int, ctypes.c_int,
                                ctypes.c_int, ctypes.c_int, ctypes.c_uint)
user32.GetParent.argtypes = (wt.HWND,)
user32.GetParent.restype = wt.HWND
user32.FindWindowW.argtypes = (wt.LPCWSTR, wt.LPCWSTR)
user32.FindWindowW.restype = wt.HWND
user32.FindWindowExW.argtypes = (wt.HWND, wt.HWND, wt.LPCWSTR, wt.LPCWSTR)
user32.FindWindowExW.restype = wt.HWND
user32.GetForegroundWindow.restype = wt.HWND
user32.GetWindowRect.argtypes = (wt.HWND, ctypes.POINTER(wt.RECT))
user32.GetClassNameW.argtypes = (wt.HWND, wt.LPWSTR, ctypes.c_int)
try:
    ctypes.windll.shcore.SetProcessDpiAwareness(2)
except (AttributeError, OSError):
    user32.SetProcessDPIAware()


def color_for(pct):
    if pct < 50:
        return "#4cc38a"
    if pct < 80:
        return "#e5b842"
    return "#e5534b"


def fmt_reset(iso):
    if not iso:
        return ""
    try:
        t = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    except ValueError:
        return ""
    secs = int((t - datetime.now(timezone.utc)).total_seconds())
    if secs <= 0:
        return "сейчас"
    d, rem = divmod(secs, 86400)
    h, rem = divmod(rem, 3600)
    m = rem // 60
    if d:
        return f"{d}д {h}ч"
    if h:
        return f"{h}ч {m}м"
    return f"{m}м"


class RateLimited(RuntimeError):
    def __init__(self, retry_after):
        super().__init__("HTTP 429")
        self.retry_after = retry_after  # seconds, 0 if the server did not say


def fetch_usage():
    with open(CREDS, encoding="utf-8") as f:
        oauth = json.load(f).get("claudeAiOauth") or {}
    token = oauth.get("accessToken")
    if not token:
        raise RuntimeError("нет токена — войди в Claude Code")
    exp = oauth.get("expiresAt")
    if exp and exp / 1000 < datetime.now().timestamp():
        raise RuntimeError("токен истёк — открой Claude Code")
    req = urllib.request.Request(URL, headers={
        "Authorization": f"Bearer {token}",
        "anthropic-beta": "oauth-2025-04-20",
        "User-Agent": "claude-usage-widget",
    })
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        if e.code == 401:
            raise RuntimeError("токен истёк — открой Claude Code")
        if e.code == 429:
            try:
                retry = int(e.headers.get("Retry-After") or 0)
            except ValueError:
                retry = 0
            raise RateLimited(retry)
        raise RuntimeError(f"HTTP {e.code}")


def window_rect(hwnd):
    r = wt.RECT()
    if hwnd and user32.GetWindowRect(hwnd, ctypes.byref(r)):
        return r.left, r.top, r.right, r.bottom
    return None


def taskbar_rect():
    """(left, top, right, bottom) of the main taskbar; screen bottom if not found."""
    r = window_rect(user32.FindWindowW("Shell_TrayWnd", None))
    if r:
        return r
    sw, sh = user32.GetSystemMetrics(0), user32.GetSystemMetrics(1)
    return 0, sh - 40, sw, sh


def tray_left():
    """Left edge of the notification area (clock and tray icons), or None."""
    tray = user32.FindWindowW("Shell_TrayWnd", None)
    r = window_rect(user32.FindWindowExW(tray, None, "TrayNotifyWnd", None)) if tray else None
    return r[0] if r else None


def foreground_is_fullscreen():
    hwnd = user32.GetForegroundWindow()
    if not hwnd:
        return False
    cls = ctypes.create_unicode_buffer(64)
    user32.GetClassNameW(hwnd, cls, 64)
    if cls.value in ("Progman", "WorkerW", "Shell_TrayWnd"):
        return False
    r = wt.RECT()
    user32.GetWindowRect(hwnd, ctypes.byref(r))
    return (r.left <= 0 and r.top <= 0 and r.right >= user32.GetSystemMetrics(0)
            and r.bottom >= user32.GetSystemMetrics(1))


class Widget:
    def __init__(self):
        self.root = tk.Tk()
        self.root.overrideredirect(True)
        self.root.attributes("-topmost", True)
        self.root.configure(bg=BG)
        self.data = None
        self.status = "загрузка…"
        self.dragging = False
        self.hidden = False
        self.results = queue.Queue()
        self.pos = None  # (x, y) we placed the window at
        self.busy = False  # a request is in flight
        self.limited = 0  # consecutive HTTP 429 answers
        self.retry_at = 0.0  # time.time() before which scheduled polls are skipped

        tb = taskbar_rect()
        self.H = max(28, tb[3] - tb[1])  # exactly the taskbar height
        self.s = self.H / BASE_H
        self.W = int(2 * SEC_W * self.s)

        self.canvas = tk.Canvas(self.root, width=self.W, height=self.H, bg=BG,
                                highlightthickness=0)
        self.canvas.pack()

        self.menu = tk.Menu(self.root, tearoff=0)
        self.menu.add_command(label=self.status, state="disabled")
        self.menu.add_command(label="Обновить", command=self.refresh)
        self.topmost = tk.BooleanVar(value=True)
        self.menu.add_checkbutton(label="Поверх всех окон", variable=self.topmost,
                                  command=self.toggle_topmost)
        self.autostart = tk.BooleanVar(value=os.path.exists(STARTUP_VBS))
        self.menu.add_checkbutton(label="Запускать с Windows", variable=self.autostart,
                                  command=self.toggle_autostart)
        self.menu.add_separator()
        self.menu.add_command(label="Закрыть", command=self.quit)

        self.canvas.bind("<ButtonPress-1>", self.drag_start)
        self.canvas.bind("<B1-Motion>", self.drag_move)
        self.canvas.bind("<ButtonRelease-1>", self.drag_end)
        self.canvas.bind("<Double-Button-1>", lambda e: self.refresh())
        self.canvas.bind("<Button-3>", self.show_menu)

        self.load_state()
        self.draw()
        self.tick()
        self.poll_results()
        self.keep_on_top()

    # --- position / settings ---
    def load_state(self):
        x = None
        try:
            with open(STATE, encoding="utf-8") as f:
                s = json.load(f)
            x = s["x"]
            self.topmost.set(s.get("topmost", True))
            self.root.attributes("-topmost", self.topmost.get())
        except (OSError, ValueError, KeyError):
            pass
        if x is None:
            tl = tray_left()
            x = (tl if tl is not None else taskbar_rect()[2]) - self.W - 8
        self.dock(x)

    def save_state(self):
        try:
            with open(STATE, "w", encoding="utf-8") as f:
                json.dump({"x": self.pos[0], "topmost": self.topmost.get()}, f)
        except OSError:
            pass

    def dock(self, x):
        """Place the widget inside the taskbar: flush with its top and bottom,
        horizontally clamped to its width. Only x is free."""
        tb = taskbar_rect()
        x = max(tb[0], min(x, tb[2] - self.W))
        y = tb[1] + (tb[3] - tb[1] - self.H) // 2
        if (x, y) != self.pos:
            self.pos = (x, y)
            self.root.geometry(f"+{x}+{y}")

    def drag_start(self, e):
        self._dx = e.x
        self.dragging = True

    def drag_move(self, e):
        self.dock(e.x_root - self._dx)

    def drag_end(self, e):
        self.dragging = False
        self.save_state()

    def show_menu(self, e):
        self.menu.entryconfig(0, label=self.status)
        self.menu.tk_popup(e.x_root, e.y_root)

    def toggle_topmost(self):
        self.root.attributes("-topmost", self.topmost.get())
        self.save_state()

    def keep_on_top(self):
        """Clicking the taskbar raises it over us; push ourselves back on top.
        Hide while a fullscreen app (video, game) is in front."""
        if foreground_is_fullscreen():
            if not self.hidden:
                self.root.withdraw()
                self.hidden = True
        else:
            if self.hidden:
                self.root.deiconify()
                self.hidden = False
            if not self.dragging:
                self.dock(self.pos[0])
            if self.topmost.get() and not self.dragging:
                hwnd = user32.GetParent(self.root.winfo_id())
                # HWND_TOPMOST, SWP_NOSIZE | SWP_NOMOVE | SWP_NOACTIVATE
                user32.SetWindowPos(hwnd, -1, 0, 0, 0, 0, 0x1 | 0x2 | 0x10)
        self.root.after(1000, self.keep_on_top)

    def toggle_autostart(self):
        if self.autostart.get():
            if FROZEN:
                cmd = f'""{sys.executable}""'
            else:
                pyw = os.path.abspath(__file__)
                exe = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")
                cmd = f'""{exe}"" ""{pyw}""'
            with open(STARTUP_VBS, "w", encoding="utf-16") as f:
                f.write(f'CreateObject("WScript.Shell").Run "{cmd}", 0, False\n')
        elif os.path.exists(STARTUP_VBS):
            os.remove(STARTUP_VBS)

    def quit(self):
        self.save_state()
        self.root.destroy()

    # --- data ---
    def tick(self):
        if time.time() >= self.retry_at:  # after HTTP 429 wait before asking again
            self.refresh()
        self.root.after(POLL_MS, self.tick)

    def refresh(self):
        if self.busy:
            return
        self.busy = True

        def work():
            try:
                data, err = fetch_usage(), None
            except Exception as ex:  # network, file, parse
                data, err = None, ex
            self.results.put((data, err))  # Tk must only be touched from the main thread
        threading.Thread(target=work, daemon=True).start()

    def poll_results(self):
        while not self.results.empty():
            self.on_result(*self.results.get())
        self.root.after(200, self.poll_results)

    def on_result(self, data, err):
        self.busy = False
        if data is not None:
            self.data = data
            self.limited = 0
            self.retry_at = 0.0
            self.status = "обновлено " + datetime.now().strftime("%H:%M")
        elif isinstance(err, RateLimited):
            wait = BACKOFF_MIN[min(self.limited, len(BACKOFF_MIN) - 1)] * 60
            self.limited += 1
            self.retry_at = time.time() + max(wait, err.retry_after)
            self.status = ("лимит запросов (429), повтор после "
                           + datetime.fromtimestamp(self.retry_at).strftime("%H:%M"))
        else:
            self.status = str(err)
        self.draw()

    # --- drawing ---
    def draw(self):
        c, s = self.canvas, self.s
        c.delete("all")
        c.create_rectangle(0, 0, self.W - 1, self.H - 1, outline="#3a3c44")
        if self.data is None:
            c.create_text(self.W // 2, self.H // 2, text=self.status, fill=DIM,
                          font=("Segoe UI", -int(13 * s)), width=self.W - 8)
            return
        f_big = ("Segoe UI Semibold", -int(13 * s))
        f_small = ("Segoe UI", -int(11 * s))
        pad, bar_h = int(10 * s), max(2, int(3 * s))
        sec_w = self.W // 2
        rows = [("5 ч", "five_hour"), ("Неделя", "seven_day")]
        for i, (label, key) in enumerate(rows):
            x0, x1 = i * sec_w + pad, (i + 1) * sec_w - pad
            block = self.data.get(key) or {}
            pct = block.get("utilization")
            col = FG if pct is None else color_for(pct)
            c.create_text(x0, int(2 * s), anchor="nw", text=label, fill=FG, font=f_big)
            c.create_text(x1, int(2 * s), anchor="ne",
                          text="—" if pct is None else f"{pct:.0f}%", fill=col, font=f_big)
            by = int(21 * s)
            c.create_rectangle(x0, by, x1, by + bar_h, fill=TRACK, width=0)
            if pct:
                fill = max(2, int((x1 - x0) * min(pct, 100) / 100))
                c.create_rectangle(x0, by, x0 + fill, by + bar_h, fill=col, width=0)
            reset = fmt_reset(block.get("resets_at"))
            c.create_text(x0, self.H - 1, anchor="sw",
                          text=f"сброс {reset}" if reset else "", fill=DIM, font=f_small)
        c.create_line(sec_w, int(6 * s), sec_w, self.H - int(6 * s), fill="#3a3c44")
        if self.status.startswith("обновлено"):
            return
        # stale data: small orange dot in the corner, details in the right-click menu
        r = max(2, int(3 * s))
        c.create_oval(self.W - 2 * r - 3, 3, self.W - 3, 3 + 2 * r, fill="#e5953b", width=0)

    def run(self):
        self.root.mainloop()


if __name__ == "__main__":
    # one copy only: extra copies stack invisibly and multiply requests (HTTP 429)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _mutex = kernel32.CreateMutexW(None, False, r"Local\claude_usage_widget")
    if ctypes.get_last_error() == 183:  # ERROR_ALREADY_EXISTS
        sys.exit(0)
    Widget().run()
