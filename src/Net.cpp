#include "Net.h"
#include "Platform.h"
#include "config.h"
#include <DNSServer.h>
#if defined(SMALLTV_ESP32) || defined(SMALLTV_ESP32C2)
#include <esp_wifi.h>
#endif

static NetMode     g_mode = NET_AP;
static DNSServer   g_dns;
static String      g_hostname;
static String      g_apSsid;
static uint32_t    g_lastReconnect = 0;
static const Settings* g_cfg = nullptr;  // for runtime failover between saved networks
static int8_t      g_curNet = -1;        // settings index of the joined network
static uint32_t    g_downSince = 0;      // 0 = connected; else millis() the drop began
static bool        g_noWifiConfigured = false;  // AP entered with zero saved networks
static uint32_t    g_apStartMs = 0;
static bool        g_espNowOnly = false;         // AP self-closed; idle STA, ESP-NOW only
static volatile bool g_espNowActivity = false;   // set from the recv callback, drained in netLoop()

static void startAP(const Settings& s) {
  g_mode = NET_AP;
  WiFi.mode(WIFI_AP);
  IPAddress apIP(192, 168, 4, 1);
  WiFi.softAPConfig(apIP, apIP, IPAddress(255, 255, 255, 0));
  // Pinned to a fixed, known channel (not just the Arduino core's default) so an
  // ESP-NOW bridge can target this device deterministically without reading
  // /api/status first — see AP_ESPNOW_CHANNEL.
  if (s.apPass.length() >= 8) {
    WiFi.softAP(s.apSsid.c_str(), s.apPass.c_str(), AP_ESPNOW_CHANNEL);
  } else {
    WiFi.softAP(s.apSsid.c_str(), nullptr, AP_ESPNOW_CHANNEL);   // open AP (WPA2 needs >=8 chars)
  }
  g_apSsid = s.apSsid;
  // Captive portal: answer every DNS query with our own IP.
  g_dns.setErrorReplyCode(DNSReplyCode::NoError);
  g_dns.start(53, "*", apIP);
}

// Drop the setup AP and idle on the same channel so any ESP-NOW pairing made
// against the AP's MAC/channel (shown on-screen, see gfxApInfo) keeps working.
// Only reached when zero networks were ever saved (see netLoop) — a real,
// in-progress captive-portal setup is never cut short.
static void dropApForEspNowOnly() {
  g_dns.stop();
  WiFi.softAPdisconnect(true);
  WiFi.mode(WIFI_STA);
  WiFi.disconnect();
#if defined(SMALLTV_ESP8266)
  wifi_set_channel(AP_ESPNOW_CHANNEL);
#elif defined(SMALLTV_ESP32) || defined(SMALLTV_ESP32C2)
  esp_wifi_set_channel(AP_ESPNOW_CHANNEL, WIFI_SECOND_CHAN_NONE);
#endif
  g_mode = NET_STA;
  g_espNowOnly = true;
}

void netBegin(const Settings& s, void (*onProgress)(const char*)) {
  g_cfg = &s;
  g_hostname = s.hostname.length() ? s.hostname : String(DEFAULT_HOSTNAME);
  WiFi.persistent(false);
  WiFi.setAutoReconnect(true);
  platformSetHostname(g_hostname.c_str());

  if (s.wifiCount == 0) {
    if (onProgress) onProgress("No WiFi saved");
    startAP(s);
    g_noWifiConfigured = true;
    g_apStartMs = millis();
    return;
  }

  WiFi.mode(WIFI_STA);

  // Try order: scan once (blocking is fine here, only the boot screen is up)
  // and put the networks the scan can see first, strongest first. Unseen ones
  // (hidden SSIDs or currently out of range) go last, in config order, with a
  // shorter timeout each.
  uint8_t order[MAX_WIFI_NETS];
  bool    seen[MAX_WIFI_NETS];
  if (s.wifiCount == 1) {
    order[0] = 0;
    seen[0] = true;
  } else {
    int32_t rssi[MAX_WIFI_NETS];
    for (uint8_t i = 0; i < s.wifiCount; i++) { rssi[i] = -32768; seen[i] = false; }
    if (onProgress) onProgress("Scanning...");
    int found = WiFi.scanNetworks();
    for (int a = 0; a < found; a++)
      for (uint8_t i = 0; i < s.wifiCount; i++)
        if (WiFi.SSID(a) == s.wifi[i].ssid && WiFi.RSSI(a) > rssi[i]) {
          rssi[i] = WiFi.RSSI(a);
          seen[i] = true;
        }
    WiFi.scanDelete();

    bool used[MAX_WIFI_NETS] = {false};
    for (uint8_t k = 0; k < s.wifiCount; k++) {
      int best = -1;
      for (uint8_t i = 0; i < s.wifiCount; i++) {
        if (used[i]) continue;
        if (best < 0 ||
            (seen[i] && !seen[best]) ||
            (seen[i] == seen[best] && rssi[i] > rssi[best])) best = i;
      }
      used[best] = true;
      order[k] = (uint8_t)best;
    }
  }

  for (uint8_t k = 0; k < s.wifiCount; k++) {
    const WifiCred& n = s.wifi[order[k]];
    if (onProgress) {
      char msg[48];
      snprintf(msg, sizeof(msg), "WiFi: %s", n.ssid.c_str());
      onProgress(msg);
    }
    WiFi.begin(n.ssid.c_str(), n.pass.c_str());

    uint32_t budget = seen[order[k]] ? 15000 : 8000;
    uint32_t start = millis();
    while (WiFi.status() != WL_CONNECTED && millis() - start < budget) {
      delay(200);
      yield();
    }

    if (WiFi.status() == WL_CONNECTED) {
      g_curNet = (int8_t)order[k];
      g_mode = NET_STA;
      if (MDNS.begin(g_hostname.c_str())) {
        MDNS.addService("http", "tcp", 80);
#if WITH_USAGE
        // Discoverable usage-push service so the clawdmeter daemon can find and
        // push to every SmallTV on the LAN (no hardcoded host). TXT carries the
        // device id, firmware version, and the push path.
        MDNS.addService("clawdmeter", "tcp", 80);
        MDNS.addServiceTxt("clawdmeter", "tcp", "id",   g_hostname.c_str());
        MDNS.addServiceTxt("clawdmeter", "tcp", "ver",  FW_VERSION);
        MDNS.addServiceTxt("clawdmeter", "tcp", "path", "/api/usage");
#endif
      }
      if (onProgress) onProgress(WiFi.localIP().toString().c_str());
      return;
    }
    WiFi.disconnect();
    delay(100);
  }

  if (onProgress) onProgress("WiFi failed -> AP");
  startAP(s);
}

void netLoop() {
  if (g_mode == NET_AP) {
    g_dns.processNextRequest();
    if (g_noWifiConfigured &&
        (g_espNowActivity || millis() - g_apStartMs > AP_ESPNOW_TIMEOUT_MS)) {
      dropApForEspNowOnly();
    }
    return;
  }
  if (g_espNowOnly) return;   // idle on purpose: no reconnect attempts, radio stays put
  // STA: keep mDNS alive, nudge reconnect if we dropped. After a long outage
  // rotate through the other saved networks. Never scan here — it would block
  // the display loop and the web server; WiFi.begin is fire-and-forget and its
  // status is picked up on later passes.
  platformMdnsUpdate();
  if (WiFi.status() == WL_CONNECTED) {
    g_downSince = 0;
    return;
  }
  if (!g_downSince) g_downSince = millis();
  if (millis() - g_lastReconnect > 10000) {
    g_lastReconnect = millis();
    if (g_cfg && g_cfg->wifiCount > 1 && millis() - g_downSince > 45000) {
      g_curNet = (int8_t)((g_curNet + 1) % g_cfg->wifiCount);
      WiFi.begin(g_cfg->wifi[g_curNet].ssid.c_str(), g_cfg->wifi[g_curNet].pass.c_str());
      g_downSince = millis();   // give this candidate its own window before rotating on
    } else {
      WiFi.reconnect();         // first ~45 s: keep nudging the current network
    }
  }
}

NetMode netMode()      { return g_mode; }
bool    netConnected() { return g_mode == NET_STA && WiFi.status() == WL_CONNECTED; }

String netIP() {
  return (g_mode == NET_AP) ? WiFi.softAPIP().toString()
                            : WiFi.localIP().toString();
}

String netSSID() {
  return (g_mode == NET_AP) ? g_apSsid : WiFi.SSID();
}

int netRSSI() {
  return (g_mode == NET_STA) ? WiFi.RSSI() : 0;
}

String netMac() {
  return (g_mode == NET_AP) ? WiFi.softAPmacAddress() : WiFi.macAddress();
}

int netChannel() { return WiFi.channel(); }

bool netEspNowOnly() { return g_espNowOnly; }

// Called from the ESP-NOW receive callback context — just raises a flag.
// Actually tearing down the AP happens from netLoop() (normal loop context);
// touching WiFi.mode()/softAPdisconnect() from inside the recv callback itself
// isn't safe on either chip family.
void netNotifyEspNowActivity() {
  g_espNowActivity = true;
}
