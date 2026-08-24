#include <Arduino.h>
#include <Preferences.h>
#include <WiFi.h>
#include <WebServer.h>
#include <vector>

#include <RCSwitch.h>
#include <IRremoteESP8266.h>
#include <IRrecv.h>
#include <IRsend.h>
#include <IRutils.h>

const uint8_t RF_TX_PIN = 26;
const uint8_t RF_RX_PIN = 27;
const uint8_t IR_TX_PIN = 4;
const uint8_t IR_RX_PIN = 14;
const uint8_t BOOT_PIN = 0;

const uint16_t IR_CAPTURE_BUFFER = 1024;
const uint8_t IR_TIMEOUT_MS = 50;
const char *DEFAULT_NODE_NAME = "esp32-room-node";
const char *SETUP_AP_SSID = "rf-ir-node-setup";

WebServer server(80);
Preferences prefs;
RCSwitch rfTx = RCSwitch();
RCSwitch rfRx = RCSwitch();
IRsend irsend(IR_TX_PIN);
IRrecv irrecv(IR_RX_PIN, IR_CAPTURE_BUFFER, IR_TIMEOUT_MS, true);
decode_results irResults;
String wifiSsid;
String wifiPassword;
String nodeName = DEFAULT_NODE_NAME;
bool setupMode = false;

String jsonEscape(const String &value) {
  String escaped;
  escaped.reserve(value.length() + 4);
  for (size_t i = 0; i < value.length(); i++) {
    char c = value[i];
    if (c == '"' || c == '\\') {
      escaped += '\\';
      escaped += c;
    } else if (c == '\n') {
      escaped += "\\n";
    } else if (c == '\r') {
      escaped += "\\r";
    } else {
      escaped += c;
    }
  }
  return escaped;
}

unsigned long argUL(const String &name, unsigned long fallback) {
  if (!server.hasArg(name)) return fallback;
  return strtoul(server.arg(name).c_str(), nullptr, 10);
}

void sendJson(int code, const String &body) {
  server.sendHeader("Connection", "close");
  server.send(code, "application/json", body);
}

void sendError(int code, const String &message) {
  sendJson(code, "{\"ok\":false,\"error\":\"" + jsonEscape(message) + "\"}");
}

bool parseRawCsv(const String &csv, std::vector<uint16_t> &out) {
  int start = 0;
  while (start < csv.length()) {
    int comma = csv.indexOf(',', start);
    if (comma < 0) comma = csv.length();
    String part = csv.substring(start, comma);
    part.trim();
    if (part.length() > 0) {
      long value = part.toInt();
      if (value <= 0 || value > 65535) return false;
      out.push_back(static_cast<uint16_t>(value));
    }
    start = comma + 1;
  }
  return out.size() > 0;
}

void handleHealth() {
  IPAddress ip = setupMode ? WiFi.softAPIP() : WiFi.localIP();
  String body = "{\"ok\":true,\"name\":\"";
  body += jsonEscape(nodeName);
  body += "\",\"ip\":\"";
  body += ip.toString();
  body += "\",\"setup_mode\":";
  body += setupMode ? "true" : "false";
  body += ",\"rf_tx\":26,\"rf_rx\":27,\"ir_tx\":4,\"ir_rx\":14}";
  sendJson(200, body);
}

void handleConfigPage() {
  String html = F(
      "<!doctype html><html><head><meta name='viewport' content='width=device-width,initial-scale=1'>"
      "<title>ESP32 RF/IR setup</title><style>"
      "body{font-family:system-ui;margin:0;background:#f4f6f3;color:#17201b}"
      "main{max-width:420px;margin:40px auto;padding:18px}"
      "form{display:grid;gap:12px;background:white;border:1px solid #dbe2dc;border-radius:8px;padding:18px}"
      "label{display:grid;gap:6px;font-size:13px;font-weight:700;color:#647067}"
      "input,button{font:inherit;min-height:42px;border-radius:8px}"
      "input{border:1px solid #dbe2dc;padding:0 10px}"
      "button{border:0;background:#0f7a54;color:white;font-weight:800}"
      "</style></head><body><main><h1>ESP32 RF/IR setup</h1>"
      "<form method='post' action='/save'>"
      "<label>Wi-Fi SSID<input name='ssid' required></label>"
      "<label>Wi-Fi password<input name='password' type='password'></label>"
      "<label>Node name<input name='name' value='esp32-room-node'></label>"
      "<button type='submit'>Save and reboot</button>"
      "</form></main></body></html>");
  server.send(200, "text/html", html);
}

void handleSaveConfig() {
  if (!server.hasArg("ssid")) {
    server.send(400, "text/plain", "missing ssid");
    return;
  }

  String savedName = server.arg("name");
  savedName.trim();
  if (savedName.length() == 0) savedName = DEFAULT_NODE_NAME;

  prefs.begin("controller", false);
  prefs.putString("ssid", server.arg("ssid"));
  prefs.putString("password", server.arg("password"));
  prefs.putString("name", savedName);
  prefs.end();

  server.send(200, "text/html", "<p>Saved. Rebooting.</p>");
  delay(800);
  ESP.restart();
}

void handleSendRF() {
  if (!server.hasArg("code") || !server.hasArg("bits")) {
    sendError(400, "missing code or bits");
    return;
  }

  unsigned long code = argUL("code", 0);
  int bits = static_cast<int>(argUL("bits", 24));
  int protocol = static_cast<int>(argUL("protocol", 1));
  int pulseLength = static_cast<int>(argUL("pulse_length", 0));
  int repeat = static_cast<int>(argUL("repeat", 6));

  rfTx.setProtocol(protocol);
  if (pulseLength > 0) rfTx.setPulseLength(pulseLength);
  rfTx.setRepeatTransmit(repeat);
  rfTx.send(code, bits);

  String body = "{\"ok\":true,\"signal_type\":\"rf\",\"code\":";
  body += String(code);
  body += ",\"bits\":";
  body += String(bits);
  body += "}";
  sendJson(200, body);
}

void handleSendIRRaw() {
  String body = server.arg("plain");
  if (body.length() == 0) {
    sendError(400, "missing raw body");
    return;
  }

  std::vector<uint16_t> raw;
  if (!parseRawCsv(body, raw)) {
    sendError(400, "raw body must be comma-separated positive microseconds");
    return;
  }

  uint16_t khz = static_cast<uint16_t>(argUL("khz", 38));
  int repeat = static_cast<int>(argUL("repeat", 1));
  for (int i = 0; i < repeat; i++) {
    irsend.sendRaw(raw.data(), raw.size(), khz);
    delay(50);
  }

  String response = "{\"ok\":true,\"signal_type\":\"ir\",\"count\":";
  response += String(raw.size());
  response += ",\"khz\":";
  response += String(khz);
  response += "}";
  sendJson(200, response);
}

void handleCaptureRF() {
  unsigned long timeoutMs = argUL("timeout_ms", 8000);
  unsigned long started = millis();
  rfRx.resetAvailable();

  while (millis() - started < timeoutMs) {
    if (rfRx.available()) {
      unsigned long code = rfRx.getReceivedValue();
      unsigned int bits = rfRx.getReceivedBitlength();
      unsigned int protocol = rfRx.getReceivedProtocol();
      unsigned int pulseLength = rfRx.getReceivedDelay();
      rfRx.resetAvailable();

      if (code == 0) {
        sendError(422, "unknown rf encoding");
        return;
      }

      String body = "{\"signal_type\":\"rf\",\"code\":";
      body += String(code);
      body += ",\"bits\":";
      body += String(bits);
      body += ",\"protocol\":";
      body += String(protocol);
      body += ",\"pulse_length\":";
      body += String(pulseLength);
      body += ",\"repeat\":6}";
      sendJson(200, body);
      return;
    }
    delay(5);
  }

  sendError(408, "rf capture timeout");
}

void handleCaptureIR() {
  unsigned long timeoutMs = argUL("timeout_ms", 10000);
  unsigned long started = millis();
  irrecv.resume();

  while (millis() - started < timeoutMs) {
    if (irrecv.decode(&irResults)) {
      String body = "{\"signal_type\":\"ir\",\"raw\":[";
      for (uint16_t i = 1; i < irResults.rawlen; i++) {
        uint32_t duration = irResults.rawbuf[i] * kRawTick;
        if (i > 1) body += ",";
        body += String(duration);
      }
      body += "],\"khz\":38,\"repeat\":1}";
      irrecv.resume();
      sendJson(200, body);
      return;
    }
    delay(5);
  }

  sendError(408, "ir capture timeout");
}

void handleNotFound() {
  sendError(404, "not found");
}

void loadConfig() {
  pinMode(BOOT_PIN, INPUT_PULLUP);
  bool resetConfig = digitalRead(BOOT_PIN) == LOW;

  prefs.begin("controller", false);
  if (resetConfig) {
    prefs.clear();
  }
  wifiSsid = prefs.getString("ssid", "");
  wifiPassword = prefs.getString("password", "");
  nodeName = prefs.getString("name", DEFAULT_NODE_NAME);
  prefs.end();
}

bool connectWifi() {
  if (wifiSsid.length() == 0) return false;

  WiFi.mode(WIFI_STA);
  WiFi.begin(wifiSsid.c_str(), wifiPassword.c_str());
  Serial.print("WiFi");
  unsigned long started = millis();
  while (WiFi.status() != WL_CONNECTED && millis() - started < 30000) {
    delay(500);
    Serial.print(".");
  }
  Serial.println();
  if (WiFi.status() != WL_CONNECTED) {
    WiFi.disconnect(true);
    return false;
  }
  Serial.print("IP: ");
  Serial.println(WiFi.localIP());
  return true;
}

void registerApiRoutes() {
  server.on("/", HTTP_GET, handleHealth);
  server.on("/health", HTTP_GET, handleHealth);
  server.on("/send/rf", HTTP_GET, handleSendRF);
  server.on("/send/ir/raw", HTTP_POST, handleSendIRRaw);
  server.on("/capture/rf", HTTP_GET, handleCaptureRF);
  server.on("/capture/ir", HTTP_GET, handleCaptureIR);
  server.onNotFound(handleNotFound);
}

void registerSetupRoutes() {
  server.on("/", HTTP_GET, handleConfigPage);
  server.on("/save", HTTP_POST, handleSaveConfig);
  server.on("/health", HTTP_GET, handleHealth);
  server.onNotFound(handleNotFound);
}

void startSetupPortal() {
  setupMode = true;
  WiFi.mode(WIFI_AP);
  WiFi.softAP(SETUP_AP_SSID);
  registerSetupRoutes();
  server.begin();
  Serial.print("Setup AP: ");
  Serial.println(SETUP_AP_SSID);
  Serial.print("Setup URL: http://");
  Serial.println(WiFi.softAPIP());
}

void setup() {
  Serial.begin(115200);
  delay(200);

  loadConfig();

  if (!connectWifi()) {
    startSetupPortal();
    return;
  }

  rfTx.enableTransmit(RF_TX_PIN);
  rfRx.enableReceive(digitalPinToInterrupt(RF_RX_PIN));
  irsend.begin();
  irrecv.enableIRIn();

  registerApiRoutes();
  server.begin();

  Serial.println("HTTP server ready");
}

void loop() {
  server.handleClient();
}
