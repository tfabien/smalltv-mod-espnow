// bridge.cpp — USB-serial <-> ESP-NOW relay for a SmallTV ESP8266 (see README:
// its onboard USB port carries no UART data lines, so clawdmeter-daemon's
// --serial transport can't reach it directly). Flash this onto any spare ESP32
// dev board with a real USB-serial chip, plug it into the PC running the
// daemon, run `clawdmeter_daemon.py --serial <bridge COM port>`, and it relays
// each JSON line over ESP-NOW to the SmallTV.
//
// Before flashing, fill in the two constants below from the SmallTV's own web
// UI (http://<its-ip>/api/status -> "mac" and "chan"). ESP-NOW does not hop
// channels, so the bridge must be pinned to the same channel the SmallTV's
// Wi-Fi router put it on — if your router changes channel (e.g. auto-channel),
// this breaks until you re-check /api/status and reflash.
#ifdef BRIDGE_BUILD
#include <WiFi.h>
#include <esp_now.h>
#include <esp_wifi.h>

// ---- EDIT BEFORE FLASHING --------------------------------------------------
static uint8_t PEER_MAC[6] = {0x24, 0x4C, 0xAB, 0x6C, 0xAB, 0xB1};   // SmallTV "mac"
#define WIFI_CHANNEL 11                                                // SmallTV "chan"
// ---------------------------------------------------------------------------

static char   g_line[192];
static size_t g_len = 0;

void setup() {
  Serial.begin(115200);
  WiFi.mode(WIFI_STA);
  WiFi.disconnect();
  esp_wifi_set_channel(WIFI_CHANNEL, WIFI_SECOND_CHAN_NONE);
  esp_now_init();

  esp_now_peer_info_t peer = {};
  memcpy(peer.peer_addr, PEER_MAC, 6);
  peer.channel = WIFI_CHANNEL;
  peer.encrypt = false;
  esp_now_add_peer(&peer);
}

// Same line-buffering as the daemon's --serial transport: one JSON object per
// newline-terminated line, forwarded as one ESP-NOW packet (well under its
// 250-byte cap for this tiny payload).
void loop() {
  while (Serial.available()) {
    char c = (char)Serial.read();
    if (c == '\n') {
      if (g_len > 0) esp_now_send(PEER_MAC, (const uint8_t*)g_line, g_len);
      g_len = 0;
    } else if (c != '\r' && g_len + 1 < sizeof(g_line)) {
      g_line[g_len++] = c;
    }
  }
}
#endif  // BRIDGE_BUILD
