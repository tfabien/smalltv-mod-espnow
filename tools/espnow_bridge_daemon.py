#!/usr/bin/env python3
"""espnow_bridge_daemon.py — tray-icon monitor + pairing console for the
ESP-NOW bridge (src/bridge.cpp), styled after clawdmeter-daemon: one small
tool that shows what the bridge is doing and lets you (re)pair it without
reflashing.

    python espnow_bridge_daemon.py             # tray icon, pick the port from its Port menu
    python espnow_bridge_daemon.py COM3        # tray icon, connect to COM3 right away
    python espnow_bridge_daemon.py COM3 --no-tray   # headless console (port required)

Every line the bridge prints over serial (boot log, "[bridge] send ...",
"[bridge] delivery: ACKed"/"FAILED", pairing confirmations) is logged to
~/.espnow-bridge-daemon.log and shown in the tray tooltip / console.

Tray menu: Port (switch board without restarting), Pair device...,
Unpair device..., Unpair all, Send custom payload..., Open log, Quit.
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
_reader_stop: "threading.Event | None" = None
_current_port: "str | None" = None


def list_ports() -> list:
    import serial.tools.list_ports
    return sorted(p.device for p in serial.tools.list_ports.comports())


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
            if not stop.is_set():   # a real disconnect, not us closing it to switch ports
                log("Serial read error — bridge disconnected")
                state.set(connected=False, last_line="Disconnected")
            return
        if line:
            log(line)
            state.set(last_line=line)


def disconnect() -> None:
    global _ser, _reader_stop
    if _reader_stop:
        _reader_stop.set()
    with _ser_lock:
        if _ser is not None:
            try:
                _ser.close()
            except Exception:
                pass
            _ser = None


def connect(port: str) -> bool:
    """Switch to a different serial port at runtime — no restart needed."""
    global _ser, _reader_stop, _current_port
    disconnect()
    log(f"Connecting to bridge on {port}...")
    try:
        ser = serial.Serial(port, 115200, timeout=1)
    except serial.SerialException as e:
        log(f"Could not open {port}: {e}")
        state.set(connected=False, last_line=f"Error: {e}")
        return False
    with _ser_lock:
        _ser = ser
    _current_port = port
    state.set(connected=True, last_line="Connected")
    _reader_stop = threading.Event()
    threading.Thread(target=reader_loop, args=(ser, _reader_stop), daemon=True).start()
    return True


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

def _find_bold_font(size):
    from PIL import ImageFont
    for candidate in ("arialbd.ttf", "Arial Bold.ttf", "DejaVuSans-Bold.ttf",
                      "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"):
        try:
            return ImageFont.truetype(candidate, size)
        except Exception:
            continue
    return ImageFont.load_default()


def _make_icon_image(connected: bool):
    """"ESP" on black over "NOW" on red, split by a wavy line — drawn large
    then downsized so the text stays crisp at actual tray size. A small dot
    in the corner (green/gray) carries connection status, since the ESP/NOW
    colors themselves are fixed."""
    from PIL import Image, ImageDraw
    import math

    N = 256
    img = Image.new("RGB", (N, N), (0, 0, 0))
    d = ImageDraw.Draw(img)

    base, amp, period = N // 2, 10, N / 1.5
    wave = [(x, base + amp * math.sin(2 * math.pi * x / period)) for x in range(0, N + 1, 4)]
    d.polygon([(0, N), *wave, (N, N)], fill=(214, 30, 30))
    d.line(wave, fill=(255, 255, 255), width=5, joint="curve")

    font = _find_bold_font(72)
    d.text((N / 2, base / 2), "ESP", font=font, fill=(255, 255, 255), anchor="mm")
    d.text((N / 2, base + (N - base) / 2), "NOW", font=font, fill=(255, 255, 255), anchor="mm")

    dot_color = (70, 200, 90) if connected else (140, 140, 140)
    d.ellipse((N - 56, N - 56, N - 16, N - 16), fill=dot_color, outline=(0, 0, 0), width=4)

    return img.resize((64, 64), Image.LANCZOS)


def _run_dialog(title: str, fields: list, initial: list, width: int = 32) -> int:
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
        e = tk.Entry(root, textvariable=v, width=width)
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


def run_with_tray() -> None:
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

    def on_send_custom(icon, item):
        vals = _open_dialog("send", "espnow-bridge — send custom payload",
                             ["Payload (sent as-is, one line, to every paired peer):"],
                             ['{"s":42,"sr":123,"w":7,"wr":5555,"st":"allowed","ok":true}'])
        if not vals:
            return
        payload = vals[0].strip()
        if payload:
            send_line(payload)

    def on_quit(icon, item):
        icon.stop()

    def port_item(p):
        # Bind `p` via a closure (def), not a default arg, so pystray's 2-arg
        # action callback stays (icon, item) — same pattern as clawdmeter-daemon.
        def _select(icon, item):
            connect(p)
        return pystray.MenuItem(p, _select, checked=lambda item: _current_port == p, radio=True)

    def port_items():
        ports = list_ports()
        if not ports:
            return [pystray.MenuItem("(no ports found)", None, enabled=False)]
        return [port_item(p) for p in ports]

    menu = pystray.Menu(
        pystray.MenuItem(lambda _: f"espnow-bridge — {_current_port or '(no port)'}", None, enabled=False),
        pystray.MenuItem(lambda _: state.get()[1][:40], None, enabled=False),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Port", pystray.Menu(port_items)),
        pystray.MenuItem("Pair device...", on_pair),
        pystray.MenuItem("Unpair device...", on_unpair),
        pystray.MenuItem("Unpair all", on_unpair_all),
        pystray.MenuItem("Send custom payload...", on_send_custom),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Open log", on_open_log),
        pystray.MenuItem("Quit", on_quit),
    )
    icons = {True: _make_icon_image(True), False: _make_icon_image(False)}
    icon = pystray.Icon("espnow-bridge", icon=icons[False], title="espnow-bridge — connecting...", menu=menu)

    def updater():
        last_connected = None
        while True:
            connected, last_line = state.get()
            if connected != last_connected:
                last_connected = connected
                icon.icon = icons[connected]
            icon.title = f"espnow-bridge ({_current_port or '?'})\n{last_line}"[:127]
            time.sleep(1)

    threading.Thread(target=updater, daemon=True).start()
    icon.run()


def tray_backend_available() -> bool:
    if sys.platform in ("win32", "darwin"):
        return True
    return bool(os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))


def main() -> None:
    ap = argparse.ArgumentParser(description="Monitor and pair the ESP-NOW bridge.")
    ap.add_argument("port", nargs="?", default=None,
                    help="e.g. COM3, /dev/ttyUSB0 — omit to pick one later from the tray's Port menu")
    ap.add_argument("--no-tray", action="store_true", help="run headless in the console")
    args = ap.parse_args()

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

    if not args.port and not use_tray:
        sys.exit("A port is required in --no-tray mode (no menu to pick one from).")
    if args.port:
        connect(args.port)
    else:
        log(f"No port given — pick one from the tray's Port menu. Available: {list_ports() or 'none found'}")

    try:
        if use_tray:
            run_with_tray()
        else:
            run_console()
    finally:
        disconnect()
        log("Stopped")


if __name__ == "__main__":
    if len(sys.argv) >= 3 and sys.argv[1] == DIALOG_FLAG:
        kind = sys.argv[2]
        if kind == "pair":
            sys.exit(_run_dialog("espnow-bridge — pair device",
                                  ["MAC (AA:BB:CC:DD:EE:FF), or FF:FF:FF:FF:FF:FF for broadcast:", "Channel:"],
                                  ["", "1"]))
        elif kind == "send":
            sys.exit(_run_dialog("espnow-bridge — send custom payload",
                                  ["Payload (sent as-is, one line, to every paired peer):"],
                                  ['{"s":42,"sr":123,"w":7,"wr":5555,"st":"allowed","ok":true}'],
                                  width=64))
        else:
            sys.exit(_run_dialog("espnow-bridge — unpair device", ["MAC (AA:BB:CC:DD:EE:FF):"], [""]))
    main()
