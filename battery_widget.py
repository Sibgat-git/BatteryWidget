"""Battery Widget - always-on-top desktop widget showing battery levels of connected devices."""
import json, os, platform, plistlib, subprocess, sys, threading, queue, time, re
import tkinter as tk

OS = platform.system()
POLL_SECONDS = 10
MISSES_BEFORE_REMOVE = 2
WIDTH = 300
APP = "BatteryWidget"

# ------------------------------------------------------------- Windows glue
def single_instance():
    if OS != "Windows":
        return True
    import ctypes
    h = ctypes.windll.kernel32.CreateMutexW(None, False, "Global\\BatteryWidgetMutex")
    single_instance.h = h                      # keep handle alive
    return ctypes.windll.kernel32.GetLastError() != 183   # ERROR_ALREADY_EXISTS

def dpi_aware():
    if OS == "Windows":
        try:
            import ctypes
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except Exception:
            pass

RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"

def launch_command():
    if getattr(sys, "frozen", False):
        return f'"{sys.executable}"'
    pyw = sys.executable.replace("python.exe", "pythonw.exe")
    return f'"{pyw}" "{os.path.abspath(__file__)}"'

def autostart_enabled():
    if OS != "Windows":
        return False
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as k:
            winreg.QueryValueEx(k, APP)
            return True
    except OSError:
        return False

def set_autostart(on):
    if OS != "Windows":
        return
    import winreg
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as k:
        if on:
            winreg.SetValueEx(k, APP, 0, winreg.REG_SZ, launch_command())
        else:
            try:
                winreg.DeleteValue(k, APP)
            except OSError:
                pass

# ------------------------------------------------------------------- config
CFG_DIR = os.path.join(os.environ.get("APPDATA", os.path.expanduser("~")), APP)
CFG_FILE = os.path.join(CFG_DIR, "config.json")

def load_cfg():
    try:
        with open(CFG_FILE) as f:
            return json.load(f)
    except Exception:
        return {}

def save_cfg(cfg):
    try:
        os.makedirs(CFG_DIR, exist_ok=True)
        with open(CFG_FILE, "w") as f:
            json.dump(cfg, f)
    except OSError:
        pass

# ---------------------------------------------------------------- providers
def run(cmd, timeout=25):
    flags = 0x08000000 if OS == "Windows" else 0
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout,
                          creationflags=flags).stdout

def windows_pnp():
    ps = (
        "$k='{104EA319-6EE2-4701-BD47-8DDBF425BBE5} 2';"
        "$r=@(Get-PnpDevice -PresentOnly -Class Bluetooth,HIDClass,Mouse,Keyboard,XboxComposite,Media "
        "-ErrorAction SilentlyContinue | ForEach-Object {"
        "$p=Get-PnpDeviceProperty -InstanceId $_.InstanceId -KeyName $k -ErrorAction SilentlyContinue;"
        "if($p -and $p.Data -ne $null){[pscustomobject]@{N=$_.FriendlyName;B=[int]$p.Data}}});"
        "ConvertTo-Json -InputObject $r -Compress"
    )
    out = run(["powershell", "-NoProfile", "-Command", ps]).strip()
    data = json.loads(out) if out else []
    if isinstance(data, dict):
        data = [data]
    return [(d["N"], d["B"], "") for d in data if d.get("N") and 0 <= d["B"] <= 100]

def windows_xinput():
    import ctypes
    class BATT(ctypes.Structure):
        _fields_ = [("BatteryType", ctypes.c_ubyte), ("BatteryLevel", ctypes.c_ubyte)]
    try:
        dll = ctypes.windll.xinput1_4
    except OSError:
        return []
    levels = {0: 5, 1: 33, 2: 66, 3: 100}
    names = {0: "Empty", 1: "Low", 2: "Medium", 3: "Full"}
    found = []
    for i in range(4):
        b = BATT()
        if dll.XInputGetBatteryInformation(i, 0, ctypes.byref(b)) != 0:
            continue
        if b.BatteryType == 1:
            found.append((f"Game Controller {i+1}", None, "Wired"))
        elif b.BatteryType in (2, 3):
            found.append((f"Game Controller {i+1}", levels[b.BatteryLevel], names[b.BatteryLevel] + " (approx.)"))
    return found

def linux_upower():
    found = []
    for path in run(["upower", "-e"]).split():
        if "battery_BAT" in path or "line_power" in path or "DisplayDevice" in path:
            continue
        info = run(["upower", "-i", path])
        pct = re.search(r"percentage:\s+(\d+)", info)
        if not pct:
            continue
        name = re.search(r"model:\s+(.+)", info) or re.search(r"native-path:\s+(.+)", info)
        found.append((name.group(1).strip() if name else path.split("/")[-1], int(pct.group(1)), ""))
    return found

def mac_ioreg():
    out = subprocess.run(["ioreg", "-r", "-k", "BatteryPercent", "-a"], capture_output=True).stdout
    if not out:
        return []
    return [(d.get("Product", "Device"), int(d["BatteryPercent"]), "")
            for d in plistlib.loads(out) if "BatteryPercent" in d]

PROVIDERS = {"Windows": [windows_pnp, windows_xinput], "Linux": [linux_upower],
             "Darwin": [mac_ioreg]}.get(OS, [])

def poll_loop(q, stop, wake):
    while not stop.is_set():
        devices = {}
        for prov in PROVIDERS:
            try:
                for name, pct, note in prov():
                    devices.setdefault(name, (pct, note))
            except Exception:
                pass
        q.put(devices)
        wake.wait(POLL_SECONDS); wake.clear()

# ----------------------------------------------------------------------- UI
BG, CARD, FG, MUTED = "#14161a", "#1e2127", "#eceff4", "#8b93a1"

def colour(p):
    return "#4cc38a" if p > 50 else "#e5b84b" if p > 20 else "#e5594b"

class Row:
    def __init__(self, parent, name):
        self.value = None
        self.f = tk.Frame(parent, bg=CARD, padx=12, pady=8)
        self.f.pack(fill="x", pady=3)
        top = tk.Frame(self.f, bg=CARD); top.pack(fill="x")
        tk.Label(top, text=name, bg=CARD, fg=FG, font=("Segoe UI", 10, "bold"), anchor="w").pack(side="left")
        self.pct = tk.Label(top, bg=CARD, fg=FG, font=("Segoe UI", 10, "bold")); self.pct.pack(side="right")
        self.bar = tk.Canvas(self.f, height=6, bg="#2b3038", highlightthickness=0)
        self.bar.pack(fill="x", pady=(6, 0))
        self.bar.bind("<Configure>", lambda e: self.draw())
        self.note = tk.Label(self.f, bg=CARD, fg=MUTED, font=("Segoe UI", 8), anchor="w")

    def draw(self):
        self.bar.delete("all")
        if self.value is not None:
            self.bar.create_rectangle(0, 0, self.bar.winfo_width() * self.value / 100, 6,
                                      fill=colour(self.value), width=0)

    def update(self, pct, note):
        self.value = pct
        self.pct.config(text="—" if pct is None else f"{pct}%", fg=FG if pct is None else colour(pct))
        if note:
            self.note.config(text=note); self.note.pack(fill="x")
        else:
            self.note.pack_forget()
        self.draw()

class Widget:
    def __init__(self, root):
        self.root, self.cfg = root, load_cfg()
        self.top = tk.BooleanVar(value=self.cfg.get("topmost", True))
        self.auto = tk.BooleanVar(value=autostart_enabled())
        if "first_run_done" not in self.cfg:         # enable start-with-Windows on first launch
            self.cfg["first_run_done"] = True
            if getattr(sys, "frozen", False) and OS == "Windows":
                try:
                    set_autostart(True); self.auto.set(True)
                except OSError:
                    pass
            save_cfg(self.cfg)

        root.overrideredirect(True)
        root.configure(bg=BG)
        root.attributes("-alpha", 0.96)
        root.attributes("-topmost", self.top.get())

        header = tk.Frame(root, bg=BG); header.pack(fill="x", padx=12, pady=(10, 0))
        tk.Label(header, text="Batteries", bg=BG, fg=FG, font=("Segoe UI", 12, "bold")).pack(side="left")
        x = tk.Label(header, text="✕", bg=BG, fg=MUTED, font=("Segoe UI", 10), cursor="hand2")
        x.pack(side="right"); x.bind("<Button-1>", lambda e: self.close())
        self.status = tk.Label(root, text="Scanning…", bg=BG, fg=MUTED, font=("Segoe UI", 8), anchor="w")
        self.status.pack(fill="x", padx=12, pady=(0, 4))
        self.list = tk.Frame(root, bg=BG); self.list.pack(fill="both", expand=True, padx=12, pady=(0, 10))

        self.menu = tk.Menu(root, tearoff=0)
        self.menu.add_checkbutton(label="Always on top", variable=self.top, command=self.toggle_top)
        self.menu.add_checkbutton(label="Start with Windows", variable=self.auto, command=self.toggle_auto)
        self.menu.add_command(label="Refresh now", command=lambda: self.wake.set())
        self.menu.add_separator()
        self.menu.add_command(label="Quit", command=self.close)

        root.bind_all("<ButtonPress-1>", self.drag_start)
        root.bind_all("<B1-Motion>", self.drag_move)
        root.bind_all("<ButtonRelease-1>", lambda e: self.save_pos())
        root.bind_all("<Button-3>", lambda e: self.menu.tk_popup(e.x_root, e.y_root))

        self.px, self.py = self.cfg.get("x"), self.cfg.get("y")
        if self.px is None:
            self.px = root.winfo_screenwidth() - WIDTH - 24
            self.py = 60
        self.place_window()

        self.rows, self.misses = {}, {}
        self.q, self.stop, self.wake = queue.Queue(), threading.Event(), threading.Event()
        threading.Thread(target=poll_loop, args=(self.q, self.stop, self.wake), daemon=True).start()
        self.pump()

    def place_window(self):
        self.root.update_idletasks()
        h = self.root.winfo_reqheight()
        self.root.geometry(f"{WIDTH}x{h}+{self.px}+{self.py}")

    def drag_start(self, e):
        self.ox, self.oy = e.x_root - self.root.winfo_x(), e.y_root - self.root.winfo_y()

    def drag_move(self, e):
        self.px, self.py = e.x_root - self.ox, e.y_root - self.oy
        self.root.geometry(f"+{self.px}+{self.py}")

    def save_pos(self):
        self.cfg.update(x=self.px, y=self.py); save_cfg(self.cfg)

    def toggle_top(self):
        self.root.attributes("-topmost", self.top.get())
        self.cfg["topmost"] = self.top.get(); save_cfg(self.cfg)

    def toggle_auto(self):
        try:
            set_autostart(self.auto.get())
        except OSError:
            self.auto.set(not self.auto.get())

    def pump(self):
        try:
            while True:
                self.apply(self.q.get_nowait())
        except queue.Empty:
            pass
        self.root.after(300, self.pump)

    def apply(self, devices):
        for name, (pct, note) in sorted(devices.items()):
            self.misses[name] = 0
            if name not in self.rows:
                self.rows[name] = Row(self.list, name)
            self.rows[name].update(pct, note)
        for name in list(self.rows):
            if name not in devices:
                self.misses[name] = self.misses.get(name, 0) + 1
                if self.misses[name] >= MISSES_BEFORE_REMOVE:
                    self.rows.pop(name).f.destroy()
        n = len(self.rows)
        self.status.config(text=f"{n} device{'s' if n != 1 else ''} · {time.strftime('%H:%M:%S')}"
                           if n else "No devices reporting a battery")
        self.place_window()

    def close(self):
        self.stop.set(); self.root.destroy()

if __name__ == "__main__":
    if not single_instance():
        sys.exit(0)
    dpi_aware()
    root = tk.Tk()
    Widget(root)
    root.mainloop()
