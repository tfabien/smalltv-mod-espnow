#include "UsageClient.h"
#include "Platform.h"
#include <ArduinoJson.h>
#include <math.h>

static UsageData g_usage;
static uint32_t  g_nextPollMs = 0;
static bool      g_inited = false;

// ---------------------------------------------------------------------------
void usageInit(const Settings& s) {
  (void)s;
  g_usage.clear();
  g_nextPollMs = millis();
  g_inited = true;
  usageEspNowBegin();
}

void usageForceRefresh() { g_nextPollMs = millis(); }

const UsageData& usageGet() { return g_usage; }

bool usageFresh(uint32_t withinMs) {
  return g_usage.valid && (millis() - g_usage.lastOkMs) <= withinMs;
}

// ---- parse: usage contract -------------------------------------------------
// { "s":29, "sr":142, "w":4, "wr":9876, "st":"allowed", "ok":true }
//   s  = 5h utilization %        sr = minutes until 5h reset
//   w  = 7d utilization %        wr = minutes until 7d reset
//   st = rate-limit status       ok = false => explicit "no data"
static void usageFilter(JsonDocument& f) {
  f["s"] = true; f["sr"] = true; f["w"] = true;
  f["wr"] = true; f["st"] = true; f["ok"] = true;
}

static bool applyUsageDoc(UsageData& d, JsonDocument& doc) {
  if (doc["ok"].is<bool>() && doc["ok"].as<bool>() == false) return false;
  if (!doc["s"].is<float>() && !doc["s"].is<int>()) return false;   // require at least session %

  d.sessionPct      = constrain(doc["s"].as<float>(), 0.0f, 100.0f);
  d.weeklyPct       = constrain(doc["w"] | 0.0f, 0.0f, 100.0f);
  d.sessionResetMin = doc["sr"] | 0;
  d.weeklyResetMin  = doc["wr"] | 0;
  strlcpy(d.status, doc["st"] | "", sizeof(d.status));

  d.valid = true;
  d.error = false;
  d.lastOkMs = millis();
  return true;
}

static bool parseUsage(UsageData& d, Stream& stream) {
  JsonDocument filter; usageFilter(filter);
  JsonDocument doc;
  if (deserializeJson(doc, stream, DeserializationOption::Filter(filter))) return false;
  return applyUsageDoc(d, doc);
}

// Pushed payload (POST /api/usage): same contract, parsed from a String body.
bool usageApply(const String& body) {
  JsonDocument filter; usageFilter(filter);
  JsonDocument doc;
  if (deserializeJson(doc, body, DeserializationOption::Filter(filter))) return false;
  return applyUsageDoc(g_usage, doc);
}

// ---- ESP-NOW receive: bridge relays the daemon's serial JSON lines here ----
// A bridge board (src/bridge.cpp) forwards clawdmeter-daemon's --serial JSON
// lines over ESP-NOW, so any of the three targets can get usage data without
// ever joining Wi-Fi — the point on a locked-down/filtered network (802.1X,
// captive portal, client isolation) where getting a random IoT device onto
// the LAN at all is the actual obstacle, not just USB-serial availability.
// Same {s,sr,w,wr,st,ok} payload, one line per ESP-NOW packet either way; only
// the receive-callback API differs per chip family.
#if defined(SMALLTV_ESP8266)
#include <espnow.h>

static void onEspNowRecv(uint8_t* mac, uint8_t* data, uint8_t len) {
  (void)mac;
  char buf[192];
  size_t n = len < sizeof(buf) - 1 ? len : sizeof(buf) - 1;
  memcpy(buf, data, n);
  buf[n] = 0;
  usageApply(String(buf));
}

void usageEspNowBegin() {
  static bool inited = false;
  if (inited) return;
  if (esp_now_init() != 0) return;
  esp_now_set_self_role(ESP_NOW_ROLE_SLAVE);
  esp_now_register_recv_cb(onEspNowRecv);
  inited = true;
}

#elif defined(SMALLTV_ESP32) || defined(SMALLTV_ESP32C2)
#include <esp_now.h>

// Arduino-ESP32 core 3.x's esp_now_recv_cb_t takes an esp_now_recv_info*
// (sender address + RSSI metadata), not a bare MAC pointer like the older
// IDF/ESP8266 signature.
static void onEspNowRecv(const esp_now_recv_info* info, const uint8_t* data, int len) {
  (void)info;
  char buf[192];
  size_t n = (size_t)len < sizeof(buf) - 1 ? (size_t)len : sizeof(buf) - 1;
  memcpy(buf, data, n);
  buf[n] = 0;
  usageApply(String(buf));
}

void usageEspNowBegin() {
  static bool inited = false;
  if (inited) return;
  if (esp_now_init() != 0) return;
  esp_now_register_recv_cb(onEspNowRecv);
  inited = true;
}

#else
void usageEspNowBegin() {}
#endif

// ---- one HTTP(S) GET + parse (mirrors StockClient::fetchUrl) ----------------
static bool fetchUsage(const Settings& s) {
  const String& url = s.usage.usageUrl;
  if (url.length() < 8) return false;
  bool https = url.startsWith("https://");

  std::unique_ptr<NetClient> client;
  if (https) {
    if (ESP.getFreeHeap() < 20000) return false;   // too little heap for TLS (incl. the 9 KB thunk); skip, don't crash
    client.reset(platformMakeSecureClient(2048));   // LAN / self-hosted endpoint
  } else {
    client.reset(new WiFiClient());
  }

  HTTPClient http;
  http.setTimeout(s.httpTimeout);
  http.setReuse(false);
  if (!http.begin(*client, url)) return false;
  http.addHeader("Accept", "application/json");

  int code = http.GET();
  if (code != HTTP_CODE_OK) { http.end(); return false; }

  bool ok = parseUsage(g_usage, http.getStream());
  http.end();
  return ok;
}

// ---------------------------------------------------------------------------
void usageService(const Settings& s) {
  if (!g_inited) usageInit(s);
  if ((int32_t)(millis() - g_nextPollMs) < 0) return;

  if (!fetchUsage(s)) g_usage.error = true;   // keep stale data, flag the error

  g_nextPollMs = millis() + (uint32_t)s.usage.pollSec * 1000UL;
}
