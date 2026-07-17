#!/usr/bin/env python3
"""tools/pair_bridge.py — pair (or unpair) the ESP-NOW bridge with one or more
SmallTVs over serial, no reflash needed (the bridge saves its peer list to
NVS and reuses it after a reboot). Get each MAC/channel from the SmallTV's
setup screen or http://<its-ip>/api/status.

    python tools/pair_bridge.py COM3 1 AA:BB:CC:DD:EE:FF
    python tools/pair_bridge.py COM3 1 AA:BB:CC:DD:EE:FF 11:22:33:44:55:66
    python tools/pair_bridge.py COM3 1 FF:FF:FF:FF:FF:FF   # broadcast: every
                                                            # listener on ch1,
                                                            # no per-device pairing
    python tools/pair_bridge.py COM3 --unpair AA:BB:CC:DD:EE:FF
    python tools/pair_bridge.py COM3 --unpair-all

All paired peers share one radio channel — that's a physical ESP-NOW
constraint, not a bridge limitation.

Requires: pip install pyserial
"""
import sys
import time

try:
    import serial
except ImportError:
    sys.exit("pyserial not installed — run: pip install pyserial")


def send_lines(port, lines):
    with serial.Serial(port, 115200, timeout=2) as ser:
        time.sleep(2)   # let the board's USB-CDC settle right after opening the port
        for line in lines:
            ser.write((line + "\n").encode())
            ser.flush()
            time.sleep(0.2)
        time.sleep(0.3)
        while ser.in_waiting:
            print(ser.readline().decode(errors="replace").rstrip())


def main():
    args = sys.argv[1:]
    if len(args) < 2:
        sys.exit(__doc__)
    port = args[0]

    if args[1] == "--unpair-all":
        send_lines(port, ["UNPAIR ALL"])
    elif args[1] == "--unpair":
        send_lines(port, [f"UNPAIR {mac}" for mac in args[2:]])
    else:
        chan = args[1]
        macs = args[2:]
        if not macs:
            sys.exit(__doc__)
        send_lines(port, [f"PAIR {mac} {chan}" for mac in macs])


if __name__ == "__main__":
    main()
