// bridge.cpp — USB-serial <-> ESP-NOW relay for one or more SmallTVs whose USB
// port has no UART data lines, or that you'd rather never join Wi-Fi at all.
// Flash this onto any spare ESP32 dev board with a real USB-serial chip, plug
// it into the PC running the daemon, run
// `clawdmeter_daemon.py --serial <bridge COM port>`, and it relays each JSON
// line over ESP-NOW to every paired peer.
//
// Pairing needs no reflash — serial commands, saved to NVS, survive reboots.
// tools/pair_bridge.py sends these for you:
//   PAIR <mac> <chan>     add a peer (the first one also sets the shared
//                         channel — see the limitation note below)
//   PAIR FF:FF:FF:FF:FF:FF <chan>
//                         "peer" = broadcast: every ESP-NOW listener on that
//                         channel gets it, no per-device pairing at all
//   UNPAIR <mac>          remove one peer
//   UNPAIR ALL            remove every peer
//
// DEFAULT_PEER_MAC/DEFAULT_CHANNEL below are only the very-first-boot
// fallback, used until a PAIR command (or previously saved ones) take over.
//
// ESP-NOW does not hop channels: one radio, one channel, shared by every
// peer. If a SmallTV's channel changes (Wi-Fi router re-picks one, or its
// setup AP channel), re-pair it with the new value — no reflash either way.
#ifdef BRIDGE_BUILD
#include <WiFi.h>
#include <esp_now.h>
#include <esp_wifi.h>
#include <Preferences.h>

static uint8_t DEFAULT_PEER_MAC[6] = {0x23, 0x4C, 0xAB, 0x6C, 0xAB, 0xB1};
#define DEFAULT_CHANNEL 1
#define MAX_PEERS 8

static Preferences prefs;
static uint8_t g_peers[MAX_PEERS][6];
static uint8_t g_peerCount = 0;
static int     g_channel;

static char   g_line[192];
static size_t g_len = 0;

static bool parseMac(const char* s, uint8_t* out) {
  unsigned b[6];
  if (sscanf(s, "%x:%x:%x:%x:%x:%x", &b[0], &b[1], &b[2], &b[3], &b[4], &b[5]) != 6)
    return false;
  for (int i = 0; i < 6; i++) out[i] = (uint8_t)b[i];
  return true;
}

static void printMac(const uint8_t* m) {
  Serial.printf("%02X:%02X:%02X:%02X:%02X:%02X", m[0], m[1], m[2], m[3], m[4], m[5]);
}

static int findPeer(const uint8_t* mac) {
  for (uint8_t i = 0; i < g_peerCount; i++)
    if (memcmp(g_peers[i], mac, 6) == 0) return i;
  return -1;
}

static void savePeers() {
  String s;
  for (uint8_t i = 0; i < g_peerCount; i++) {
    char buf[18];
    snprintf(buf, sizeof(buf), "%02X:%02X:%02X:%02X:%02X:%02X",
             g_peers[i][0], g_peers[i][1], g_peers[i][2], g_peers[i][3], g_peers[i][4], g_peers[i][5]);
    if (i) s += ",";
    s += buf;
  }
  prefs.putString("peers", s);
  prefs.putInt("chan", g_channel);
}

static void addPeer(const uint8_t* mac, bool persist) {
  if (findPeer(mac) >= 0) return;
  if (g_peerCount >= MAX_PEERS) { Serial.println("[bridge] peer list full (8 max)"); return; }
  esp_now_peer_info_t peer = {};
  memcpy(peer.peer_addr, mac, 6);
  peer.channel = g_channel;
  peer.encrypt = false;
  Serial.print("[bridge] add peer "); printMac(mac);
  Serial.printf(" ch%d: %s\n", g_channel, esp_now_add_peer(&peer) == ESP_OK ? "ok" : "FAILED");
  memcpy(g_peers[g_peerCount++], mac, 6);
  if (persist) savePeers();
}

static void removePeer(const uint8_t* mac) {
  int i = findPeer(mac);
  if (i < 0) { Serial.println("[bridge] not paired, nothing to remove"); return; }
  esp_now_del_peer(g_peers[i]);
  for (uint8_t k = i; k < g_peerCount - 1; k++) memcpy(g_peers[k], g_peers[k + 1], 6);
  g_peerCount--;
  savePeers();
  Serial.print("[bridge] removed peer "); printMac(mac); Serial.println();
}

static void removeAllPeers() {
  for (uint8_t i = 0; i < g_peerCount; i++) esp_now_del_peer(g_peers[i]);
  g_peerCount = 0;
  savePeers();
  Serial.println("[bridge] all peers removed");
}

static void onSent(const uint8_t* mac, esp_now_send_status_t status) {
  (void)mac;
  Serial.printf("[bridge] delivery: %s\n", status == ESP_NOW_SEND_SUCCESS ? "ACKed" : "FAILED (no ack)");
}

void setup() {
  Serial.begin(115200);
  Serial.println("[bridge] booting");
  WiFi.mode(WIFI_STA);
  WiFi.disconnect();
  Serial.printf("[bridge] esp_now_init: %s\n", esp_now_init() == ESP_OK ? "ok" : "FAILED");
  esp_now_register_send_cb(onSent);

  prefs.begin("bridge", false);
  g_channel = prefs.getInt("chan", DEFAULT_CHANNEL);
  esp_wifi_set_channel(g_channel, WIFI_SECOND_CHAN_NONE);

  String saved = prefs.getString("peers", "");
  int start = 0;
  while (start < (int)saved.length()) {
    int comma = saved.indexOf(',', start);
    String tok = comma < 0 ? saved.substring(start) : saved.substring(start, comma);
    uint8_t mac[6];
    if (parseMac(tok.c_str(), mac)) addPeer(mac, /*persist=*/false);
    if (comma < 0) break;
    start = comma + 1;
  }
  if (g_peerCount == 0) {
    Serial.println("[bridge] no saved peers yet, using compiled-in default "
                    "(send: PAIR AA:BB:CC:DD:EE:FF <channel>)");
    addPeer(DEFAULT_PEER_MAC, /*persist=*/false);
  }
  Serial.printf("[bridge] own MAC: %s  %d peer(s) ready, waiting for serial lines\n",
                WiFi.macAddress().c_str(), g_peerCount);
}

// Same line-buffering as the daemon's --serial transport: one line per
// newline. PAIR/UNPAIR lines manage the peer list and persist it; anything
// else is forwarded as one ESP-NOW packet per peer (well under its 250-byte
// cap for this tiny payload).
void loop() {
  while (Serial.available()) {
    char c = (char)Serial.read();
    if (c == '\n') {
      if (g_len > 0) {
        g_line[g_len] = 0;
        char macStr[18];
        int chan;
        if (strncmp(g_line, "PAIR ", 5) == 0 &&
            sscanf(g_line + 5, "%17s %d", macStr, &chan) == 2) {
          uint8_t mac[6];
          if (parseMac(macStr, mac)) {
            if (g_peerCount == 0) { g_channel = chan; esp_wifi_set_channel(g_channel, WIFI_SECOND_CHAN_NONE); }
            else if (chan != g_channel) Serial.printf("[bridge] warning: ignoring ch%d, already on ch%d "
                                                        "(one radio, one channel for every peer)\n", chan, g_channel);
            addPeer(mac, /*persist=*/true);
          } else {
            Serial.println("[bridge] bad PAIR line, expected: PAIR AA:BB:CC:DD:EE:FF <channel>");
          }
        } else if (strncmp(g_line, "UNPAIR ALL", 10) == 0) {
          removeAllPeers();
        } else if (strncmp(g_line, "UNPAIR ", 7) == 0) {
          uint8_t mac[6];
          if (parseMac(g_line + 7, mac)) removePeer(mac);
          else Serial.println("[bridge] bad UNPAIR line, expected: UNPAIR AA:BB:CC:DD:EE:FF");
        } else {
          for (uint8_t i = 0; i < g_peerCount; i++) {
            esp_err_t err = esp_now_send(g_peers[i], (const uint8_t*)g_line, g_len);
            Serial.print("[bridge] send to "); printMac(g_peers[i]);
            Serial.printf(": %s\n", err == ESP_OK ? "queued ok" : "FAILED to queue");
          }
        }
      }
      g_len = 0;
    } else if (c != '\r' && g_len + 1 < sizeof(g_line)) {
      g_line[g_len++] = c;
    }
  }
}
#endif  // BRIDGE_BUILD
