#!/usr/bin/env python3
"""espnow_bridge_daemon.py — tray-icon monitor + pairing console for the
ESP-NOW bridge (src/bridge.cpp), styled after clawdmeter-daemon: one small
tool that shows what the bridge is doing and lets you (re)pair it without
reflashing.

    python espnow_bridge_daemon.py COM3                # tray icon (default)
    python espnow_bridge_daemon.py COM3 --no-tray       # headless console

Every line the bridge prints over serial (boot log, "[bridge] send ...",
"[bridge] delivery: ACKed"/"FAILED", pairing confirmations) is logged to
~/.espnow-bridge-daemon.log and shown in the tray tooltip / console.

Tray menu: Pair device..., Unpair device..., Unpair all, Open log, Quit.
Headless console: type any line the bridge itself understands (PAIR ..,
UNPAIR .., or a raw payload line) and it's sent straight through.

Requires: pip install pyserial pystray Pillow   (pystray/Pillow optional —
falls back to headless if missing, same as clawdmeter-daemon)
"""
import argparse
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

try:
    import serial
except ImportError:
    sys.exit("pyserial not installed — run: pip install pyserial")

LOG_PATH = Path.home() / ".espnow-bridge-daemon.log"
DIALOG_FLAG = "--_dialog"   # re-invoke ourselves for a Tk dialog on its own main thread

_log_lock = threading.Lock()


def log(msg: str) -> None:
    line = f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}"
    with _log_lock:
        print(line, flush=True)
        try:
            with LOG_PATH.open("a", encoding="utf-8") as f:
                f.write(line + "\n")
        except OSError:
            pass


class State:
    def __init__(self):
        self.lock = threading.Lock()
        self.connected = False
        self.last_line = "Connecting..."

    def set(self, connected=None, last_line=None):
        with self.lock:
            if connected is not None:
                self.connected = connected
            if last_line is not None:
                self.last_line = last_line

    def get(self):
        with self.lock:
            return self.connected, self.last_line


state = State()
_ser: "serial.Serial | None" = None
_ser_lock = threading.Lock()


def send_line(line: str) -> None:
    with _ser_lock:
        if _ser is None:
            log("Not connected — nothing sent")
            return
        try:
            _ser.write((line + "\n").encode())
            _ser.flush()
            log(f"> {line}")
        except serial.SerialException as e:
            log(f"Write failed: {e}")


def reader_loop(ser: "serial.Serial", stop: threading.Event) -> None:
    while not stop.is_set():
        try:
            line = ser.readline().decode("utf-8", errors="replace").rstrip()
        except serial.SerialException:
            log("Serial read error — bridge disconnected")
            state.set(connected=False, last_line="Disconnected")
            stop.set()
            return
        if line:
            log(line)
            state.set(last_line=line)


# ---- headless console ------------------------------------------------------

def run_console() -> None:
    log("Type PAIR/UNPAIR commands or any payload line; Ctrl+C to quit.")
    try:
        while True:
            try:
                cmd = input()
            except EOFError:
                break
            if cmd.strip().lower() in ("quit", "exit"):
                break
            if cmd:
                send_line(cmd)
    except KeyboardInterrupt:
        pass


# ---- tray icon --------------------------------------------------------------

def _make_icon_image(color):
    from PIL import Image, ImageDraw
    img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.ellipse((8, 8, 56, 56), fill=color)
    return img


def _run_dialog(title: str, fields: list, initial: list) -> int:
    """Standalone input dialog (its own process/main thread — a Tk window made
    on pystray's callback thread renders but can't take keyboard input)."""
    try:
        import tkinter as tk
    except Exception as e:
        sys.stderr.write(f"tkinter unavailable: {e}\n")
        return 1

    result = {"val": None}
    root = tk.Tk()
    root.title(title)
    root.resizable(False, False)
    root.attributes("-topmost", True)

    vars_ = []
    first_entry = None
    for i, label in enumerate(fields):
        tk.Label(root, text=label).pack(padx=16, pady=(12 if i == 0 else 4, 0), anchor="w")
        v = tk.StringVar(value=initial[i] if i < len(initial) else "")
        e = tk.Entry(root, textvariable=v, width=32)
        e.pack(padx=16, pady=(0, 4), fill="x")
        vars_.append(v)
        if i == 0:
            first_entry = e

    def _ok():
        result["val"] = "\t".join(v.get() for v in vars_)
        root.destroy()

    def _cancel():
        result["val"] = None
        root.destroy()

    bar = tk.Frame(root)
    bar.pack(padx=16, pady=(8, 12), anchor="e")
    tk.Button(bar, text="OK", width=8, command=_ok).pack(side="right", padx=(8, 0))
    tk.Button(bar, text="Cancel", width=8, command=_cancel).pack(side="right")

    root.bind("<Return>", lambda e: _ok())
    root.bind("<Escape>", lambda e: _cancel())
    root.protocol("WM_DELETE_WINDOW", _cancel)

    root.update_idletasks()
    try:
        root.eval("tk::PlaceWindow . center")
    except Exception:
        pass
    root.lift()
    first_entry.focus_force()
    root.after(120, lambda: (root.focus_force(), first_entry.focus_force()))
    root.mainloop()

    if result["val"] is not None:
        sys.stdout.write("OK\n" + result["val"])
        sys.stdout.flush()
    return 0


def _open_dialog(kind: str, title: str, fields: list, initial: list):
    try:
        proc = subprocess.run(
            [sys.executable, os.path.abspath(__file__), DIALOG_FLAG, kind],
            capture_output=True, text=True, timeout=120)
    except Exception as e:
        log(f"Could not open dialog: {e}")
        return None
    out = proc.stdout or ""
    if not out.startswith("OK\n"):
        return None   # cancelled / closed
    return out[3:].split("\t")


def run_with_tray(port: str) -> None:
    import pystray

    def on_pair(icon, item):
        vals = _open_dialog("pair", "espnow-bridge — pair device",
                             ["MAC (AA:BB:CC:DD:EE:FF), or FF:FF:FF:FF:FF:FF for broadcast:", "Channel:"],
                             ["", "1"])
        if not vals:
            return
        mac = vals[0].strip()
        chan = vals[1].strip() if len(vals) > 1 else ""
        if mac and chan:
            send_line(f"PAIR {mac} {chan}")

    def on_unpair(icon, item):
        vals = _open_dialog("unpair", "espnow-bridge — unpair device",
                             ["MAC (AA:BB:CC:DD:EE:FF):"], [""])
        if not vals:
            return
        mac = vals[0].strip()
        if mac:
            send_line(f"UNPAIR {mac}")

    def on_unpair_all(icon, item):
        send_line("UNPAIR ALL")

    def on_open_log(icon, item):
        try:
            if sys.platform == "win32":
                os.startfile(LOG_PATH)  # noqa
            elif sys.platform == "darwin":
                subprocess.run(["open", str(LOG_PATH)])
            else:
                subprocess.run(["xdg-open", str(LOG_PATH)])
        except Exception as e:
            log(f"Could not open log: {e}")

    def on_quit(icon, item):
        icon.stop()

    menu = pystray.Menu(
        pystray.MenuItem(lambda _: f"espnow-bridge — {port}", None, enabled=False),
        pystray.MenuItem(lambda _: state.get()[1][:40], None, enabled=False),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Pair device...", on_pair),
        pystray.MenuItem("Unpair device...", on_unpair),
        pystray.MenuItem("Unpair all", on_unpair_all),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Open log", on_open_log),
        pystray.MenuItem("Quit", on_quit),
    )
    icons = {True: _make_icon_image((70, 200, 90, 255)), False: _make_icon_image((200, 70, 55, 255))}
    icon = pystray.Icon("espnow-bridge", icon=icons[False], title="espnow-bridge — connecting...", menu=menu)

    def updater():
        last_connected = None
        while True:
            connected, last_line = state.get()
            if connected != last_connected:
                last_connected = connected
                icon.icon = icons[connected]
            icon.title = f"espnow-bridge ({port})\n{last_line}"[:127]
            time.sleep(1)

    threading.Thread(target=updater, daemon=True).start()
    icon.run()


def tray_backend_available() -> bool:
    if sys.platform in ("win32", "darwin"):
        return True
    return bool(os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))


def main() -> None:
    ap = argparse.ArgumentParser(description="Monitor and pair the ESP-NOW bridge.")
    ap.add_argument("port", help="e.g. COM3, /dev/ttyUSB0")
    ap.add_argument("--no-tray", action="store_true", help="run headless in the console")
    args = ap.parse_args()

    log(f"Connecting to bridge on {args.port}...")
    try:
        ser = serial.Serial(args.port, 115200, timeout=1)
    except serial.SerialException as e:
        sys.exit(f"Could not open {args.port}: {e}")

    global _ser
    _ser = ser
    state.set(connected=True, last_line="Connected")

    stop = threading.Event()
    threading.Thread(target=reader_loop, args=(ser, stop), daemon=True).start()

    use_tray = not args.no_tray
    if use_tray and not tray_backend_available():
        log("No system-tray backend on this session - running headless.")
        use_tray = False
    if use_tray:
        try:
            import pystray  # noqa: F401
            from PIL import Image  # noqa: F401
        except ImportError:
            log("pystray/Pillow not installed - running headless (pip install pystray Pillow)")
            use_tray = False

    try:
        if use_tray:
            run_with_tray(args.port)
        else:
            run_console()
    finally:
        stop.set()
        ser.close()
        log("Stopped")


if __name__ == "__main__":
    if len(sys.argv) >= 3 and sys.argv[1] == DIALOG_FLAG:
        kind = sys.argv[2]
        if kind == "pair":
            sys.exit(_run_dialog("espnow-bridge — pair device",
                                  ["MAC (AA:BB:CC:DD:EE:FF), or FF:FF:FF:FF:FF:FF for broadcast:", "Channel:"],
                                  ["", "1"]))
        else:
            sys.exit(_run_dialog("espnow-bridge — unpair device", ["MAC (AA:BB:CC:DD:EE:FF):"], [""]))
    main()
