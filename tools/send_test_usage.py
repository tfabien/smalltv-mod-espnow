#!/usr/bin/env python3
"""tools/send_test_usage.py — write one usage-contract JSON line to a serial
port. Point it at the ESP-NOW bridge's COM port (or any device speaking
clawdmeter-daemon's --serial contract) to check the whole chain end to end
without running the daemon or needing a real Claude token.

    python tools/send_test_usage.py COM3
    python tools/send_test_usage.py COM3 --s 90 --w 50 --st rejected
    python tools/send_test_usage.py COM3 --ok false     # simulate "no data"

Requires: pip install pyserial
"""
import argparse
import json
import sys
import time

try:
    import serial
except ImportError:
    sys.exit("pyserial not installed — run: pip install pyserial")


def str2bool(v):
    return v.lower() not in ("false", "0", "no")


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                  formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("port", help="e.g. COM3, /dev/ttyUSB0")
    ap.add_argument("--baud", type=int, default=115200)
    ap.add_argument("--s", type=float, default=42, help="5h utilization %% (default 42)")
    ap.add_argument("--sr", type=int, default=123, help="minutes until 5h reset")
    ap.add_argument("--w", type=float, default=7, help="7d utilization %% (default 7)")
    ap.add_argument("--wr", type=int, default=5555, help="minutes until 7d reset")
    ap.add_argument("--st", default="allowed", help="rate-limit status string")
    ap.add_argument("--ok", type=str2bool, default=True, metavar="{true,false}")
    args = ap.parse_args()

    payload = {"s": args.s, "sr": args.sr, "w": args.w, "wr": args.wr,
               "st": args.st, "ok": args.ok}
    line = json.dumps(payload, separators=(",", ":")) + "\n"

    with serial.Serial(args.port, args.baud, timeout=2) as ser:
        time.sleep(2)   # let the board's USB-CDC settle right after opening the port
        ser.write(line.encode())
        ser.flush()

    print("sent:", line.strip())


if __name__ == "__main__":
    main()
