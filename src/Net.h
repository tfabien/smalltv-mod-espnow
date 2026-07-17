// Net.h — WiFi station / fallback AP / captive portal / mDNS
#pragma once
#include <Arduino.h>
#include "Settings.h"

enum NetMode { NET_STA, NET_AP };

// Connects to the configured station; falls back to AP if that fails or no
// credentials are stored. `onProgress` (optional) is called with short status
// strings so the display can show what's happening during the boot connect.
void netBegin(const Settings& s, void (*onProgress)(const char*) = nullptr);
void netLoop();           // pump DNS (AP) / mDNS (STA) / reconnect

NetMode  netMode();
bool     netConnected();  // STA associated with an IP
String   netIP();         // current IP (STA or AP)
String   netSSID();       // joined SSID (STA) or AP SSID
int      netRSSI();       // STA signal, 0 in AP mode
String   netMac();        // MAC of whichever interface is actually radiating
int      netChannel();    // current radio channel (AP or STA)

// True once the no-Wi-Fi-configured setup AP has self-closed and the radio has
// dropped to idle STA (still on the same channel) for ESP-NOW-only operation.
// main.cpp uses this to stop showing the "join our AP" screen and start
// rendering the active DisplayMode, which then relies on ESP-NOW-fed data.
bool     netEspNowOnly();

// Called from the ESP-NOW receive callback (UsageClient) on every applied
// packet. If the no-Wi-Fi-configured setup AP is still up, drops it right
// away instead of waiting out AP_ESPNOW_TIMEOUT_MS — a bridge that's already
// delivering data means there's nothing left for the AP to do. No-op
// otherwise (a real, in-progress captive-portal setup is never interrupted).
void     netNotifyEspNowActivity();
