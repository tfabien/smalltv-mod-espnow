# ESP-NOW usage transport (fork-specific)

This fork adds a fourth way to get Claude usage data onto the device, for the
one board where the existing transports don't fit: the **SmallTV ESP8266**.

## Why

[clawdmeter-daemon](https://github.com/giovi321/clawdmeter-daemon) already
supports a `--serial` transport: it writes the usage JSON as one line over a
USB-CDC serial port, meant for devices like the Clawdmeter ESP32-S3 that
expose a native USB-serial connection.

The GeekMagic SmallTV (ESP8266 / ESP-12F) can't use it: its USB port only
supplies power — there is no USB-serial chip on the board, and the ESP8266's
own UART is only reachable on internal solder pads (see the "Tell-tale" row in
the main README's board table, and the flashing table's "UART not exposed"
note). Wiring an external USB-TTL adapter to those pads works, but it means
opening the case and soldering.

This fork adds a wireless workaround instead: a small companion **bridge**
firmware, flashed onto any spare ESP32 dev board that *does* have a real
USB-serial chip, relays the daemon's serial JSON lines to the SmallTV over
**ESP-NOW** (no Wi-Fi association needed, just matching radio channel).

```
clawdmeter-daemon --serial COMx
        |  USB, JSON lines (unmodified --serial transport)
        v
  [ESP32 bridge]  (src/bridge.cpp in this fork)
        |  ESP-NOW, same JSON line as one packet
        v
  [SmallTV ESP8266]  (this firmware, modified)
```

The ESP32-C2 and classic-ESP32 SmallTV variants aren't affected — they have a
real onboard USB-serial chip (CH340C) and can already use `--serial` directly.

## What changed vs. upstream

- **`src/features/usage/UsageClient.{h,cpp}`** — added `usageEspNowBegin()`,
  compiled in only for `SMALLTV_ESP8266`. Registers an ESP-NOW receive
  callback that feeds each incoming packet straight into the existing
  `usageApply()` (the same parser the HTTP push endpoint already uses).
  Called once from `usageInit()`, guarded to run only the first time.
- **`src/WebPortal.cpp`** — `/api/status` now also returns `mac` (the
  device's Wi-Fi MAC) and `chan` (its current Wi-Fi channel), so you can read
  the two values the bridge needs to pair without opening a serial console.
- **`src/bridge.cpp`** (new) — the bridge firmware itself. Reads newline-
  terminated JSON lines from `Serial` and forwards each as one ESP-NOW packet
  to a hardcoded peer MAC/channel. Edit the `PEER_MAC` / `WIFI_CHANNEL`
  constants at the top of the file before building — see below.
- **`platformio.ini`** — new `[env:espnow_bridge]` (plain ESP32 dev board,
  builds only `bridge.cpp`, same on/off pattern as the existing
  `smalltv_loader` env). Also set `WITH_TICKER=0` / `WITH_RADAR=0` on the
  `smalltv` env to free up flash headroom — re-enable if you need those
  features too.

No changes to the usage payload contract (`{s,sr,w,wr,st,ok}`) and no changes
to `clawdmeter-daemon` itself — it already writes exactly this over
`--serial`, just pointed at the bridge's COM port instead of a device's.

## Setup

1. Flash this firmware onto your SmallTV as usual (`pio run -e smalltv -t
   upload`, or OTA via `/update`). Once it's on your Wi-Fi, open
   `http://<its-ip>/api/status` and note `mac` and `chan`.
2. Edit `PEER_MAC` and `WIFI_CHANNEL` at the top of `src/bridge.cpp` with
   those values.
3. Flash `src/bridge.cpp` onto a spare ESP32 dev board:
   `pio run -e espnow_bridge -t upload`.
4. Plug that board into the machine running clawdmeter-daemon and run it with
   `--serial <bridge's COM port>` instead of `--push` / `--serve`.

## Known limitation

ESP-NOW does not hop channels. The bridge is pinned to whatever channel your
router had the SmallTV on *at flash time*. If your router changes channel
(auto-channel, a reboot, a firmware update), the link silently stops — re-check
`/api/status` for the new `chan` and reflash the bridge. This hasn't been
worth automating yet since it's just being tested; a fix would have the
SmallTV broadcast its current channel (e.g. in its mDNS TXT record) and the
bridge read it back instead of hardcoding it.
