// UsageClient.h — pulls Claude usage from the local daemon's HTTP endpoint.
//
// The companion daemon (see daemon/) polls the Claude API rate-limit headers and
// serves the latest snapshot as a tiny JSON object. The device GETs that URL on
// its poll schedule — exactly like the stock webhook, but a different contract.
#pragma once
#include "Settings.h"
#include "UsageData.h"

void usageInit(const Settings& s);
void usageService(const Settings& s);     // call each loop; fetches on the poll schedule
void usageForceRefresh();                 // poll again on the next service() call
const UsageData& usageGet();
bool usageFresh(uint32_t withinMs);       // true if the last good update is recent enough

// Apply a usage payload PUSHED to the device (POST /api/usage) — used when the
// device can't reach the daemon (Wi-Fi client isolation) so the daemon pushes.
bool usageApply(const String& body);      // parse {s,sr,w,wr,st,ok}; true on success

// ESP-NOW receive path (all board targets): a companion bridge (src/bridge.cpp,
// any ESP32 with a real USB-serial chip) relays the daemon's --serial JSON
// lines here over ESP-NOW, so the device never has to join Wi-Fi at all — the
// point on a locked-down/filtered network, not just on boards whose own USB
// port carries no UART data lines. Idempotent; safe to call every usageInit().
void usageEspNowBegin();
