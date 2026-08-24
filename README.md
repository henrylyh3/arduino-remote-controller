# Arduino Remote Controller

Local smart-home controller for RF fans and IR air conditioners.

Flow:

```text
phone/browser -> laptop web app -> HTTP over Wi-Fi -> ESP32 room node -> RF/IR -> appliance
```

## What is included

- FastAPI backend served from the always-on laptop.
- SQLite database for nodes, learned signals, timers, workflows, schedules, and event counts.
- Browser dashboard for phones/laptops on the same Wi-Fi.
- ESP32 starter firmware for 433 MHz RF send/capture and raw IR send/capture.

This starter uses HTTP between the laptop and ESP32 nodes. MQTT can be added later if you want broker/pub-sub behavior, but it is not required for a single local controller.

## Hardware ingredients

Buy one set per room node.

Confirmed purchases from 24 Aug 2026:

| Ingredient | Shopee link | Confirmed item / variation | Use in this project | Status / note |
| --- | --- | --- | --- | --- |
| ESP32 DevKit | [ESP32 30 Pin ESP-WROOM-32 Wi-Fi Bluetooth Development Board](https://my.shp.ee/RNU8PSQR) | Type-C ESP32-WROOM-32, 30-pin | Main room node controller. Runs Wi-Fi, HTTP API, RF send/capture, and IR send/capture firmware. | Confirmed purchase. CH340C, CP2102, or CH9102 USB chip is OK. |
| 433 MHz RF transmitter + receiver | [433MHz Transmitter Receiver RF Module](https://my.shp.ee/NQVLyfFR) | TX + RX pair | RF TX sends Fanzo/RF fan commands. RF RX captures/learns the original remote signal. | Confirmed purchase. |
| NPN transistor | [Transistor 2N Series](https://my.shp.ee/Lde77knv) | 2N3904 | Drives the IR LED with more current than ESP32 GPIO can safely supply. Needed for stronger AC IR transmit range. | Confirmed purchase. Use 2N3904, not PNP parts such as 2N3906 or 2N2907. |
| IR receiver + small IR transmitter module | [HX1838 Infrared IR Receiver Sensor And LED Transmitter Module](https://my.shp.ee/7tuaENzw) | HX1838 receiver + LED transmitter module | IR receiver learns/captures AC remote signals. Small transmitter module can be used for short-range testing. | Confirmed purchase. For final 2m+ transmit, use the separate 5 mm 940 nm IR LED plus 2N3904 circuit. |
| IR emitter LED pack | [10pcs F3mm F5mm LED Infrared Emitting Diode](https://my.shp.ee/e6HjUNmi) | 5 mm IR emitter 940 nm | Final AC IR transmitter LED. Connect through 100 ohm resistor and 2N3904 transistor driver. | Confirmed purchase. Choose emitter, not receiver/photodiode/phototransistor. |
| Resistors | Not purchased yet | 1k (GPIO4 to 2N3904 base), 100 ohm (IR LED current limit), 10k (RF receiver DATA divider top), 20k or 22k (RF receiver DATA divider bottom to GND) | Protects ESP32 GPIO and drives the IR LED circuit correctly. | Required. |
| Jumper wires | Not purchased yet | Male-to-male and/or female-to-female wires | Temporary wiring between ESP32, modules, resistor divider, and IR driver. | Required for testing. |
| Breadboard | Not purchased yet | Small solderless breadboard | Testing circuit before final build. | Required for easy setup/debug. |

If you use a ready-made IR transmitter module, connect its signal pin to GPIO4, VCC to 3.3V first, and GND to GND. For reliable 2m or longer AC control, use the 5 mm 940 nm IR LED plus transistor circuit below.

## Circuit diagram

![ESP32 RF and IR room node circuit diagram](docs/circuit-diagram.svg)

Connection summary:

| Part | Pin | Connect to |
| --- | --- | --- |
| RF transmitter | DATA | ESP32 GPIO26 |
| RF transmitter | VCC | ESP32 5V/VIN |
| RF transmitter | GND | ESP32 GND |
| RF receiver | DATA | 10k resistor, then ESP32 GPIO27; add 20k/22k from GPIO27 side to GND |
| RF receiver | VCC | ESP32 5V/VIN |
| RF receiver | GND | ESP32 GND |
| IR receiver | OUT/SIGNAL | ESP32 GPIO14 |
| IR receiver | VCC | ESP32 3V3 |
| IR receiver | GND | ESP32 GND |
| IR LED driver | ESP32 GPIO4 | 1k resistor to 2N3904 base |
| IR LED driver | 2N3904 emitter | ESP32 GND |
| IR LED driver | 2N3904 collector | IR LED short leg |
| IR LED driver | IR LED long leg | 100 ohm resistor to ESP32 5V/VIN |

Important: ESP32 GPIO is not 5V tolerant. If the RF receiver is powered from 5V, use the voltage divider before GPIO27.

## Laptop setup

```bash
cd backend
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

Open from the laptop:

```text
http://127.0.0.1:8000
```

Open from a phone on the same Wi-Fi:

```text
http://<laptop-lan-ip>:8000
```

Find the laptop LAN IP on macOS:

```bash
ipconfig getifaddr en0
```

The SQLite file is created at `backend/smart_home.sqlite3`. Override with:

```bash
SMART_HOME_DB=/path/to/controller.sqlite3 uvicorn app.main:app --host 0.0.0.0 --port 8000
```

Default schedule timezone is `Asia/Kuala_Lumpur`. Override with:

```bash
APP_TIMEZONE=Asia/Kuala_Lumpur uvicorn app.main:app --host 0.0.0.0 --port 8000
```

## Timers and workflows

Single timer:

```text
after 30 minutes -> press Fan Off
```

Workflow:

```text
after 30 minutes -> press Fan Speed 1
after 1 hour -> press Fan Off
```

Immediate next action:

```text
after 30 minutes -> press Fan Speed 1
0 minutes -> press Fan Off
```

Workflow step delays are relative. The next delay starts after the previous action succeeds. If one step fails, later steps are cancelled.
Saved workflows can be edited from the dashboard. Edits affect future runs, not runs already created.

Workflow schedule:

```text
daily at 22:30 -> start Night routine
Mon, Tue, Wed, Thu, Fri at 07:00 -> start Morning routine
```

If the first workflow step is `0 minutes`, the first action runs at the scheduled time.
The dashboard shows active jobs in one list: pending timers, enabled schedules, enabled workflow schedules, and pending/running workflow runs.

## Learn signals

Add the ESP32 room node, then use `Learn signal`.

```text
name: Bedroom fan power
node: Bedroom ESP32
signal: RF 433MHz
```

When capture succeeds, the learned signal is saved as a dashboard button automatically. Name the signal with the device/action you want to recognise later, such as `Bedroom AC Cool 24` or `Living fan speed 2`.

## ESP32 setup

Arduino IDE:

- Board package: ESP32 by Espressif
- Board: ESP32 Dev Module
- Libraries:
  - `rc-switch`
  - `IRremoteESP8266`

Firmware:

```text
firmware/esp32_room_node/esp32_room_node.ino
```

First boot Wi-Fi setup:

1. Upload the firmware.
2. Connect a phone/laptop to the `rf-ir-node-setup` Wi-Fi network.
3. Open `http://192.168.4.1`.
4. Save home Wi-Fi SSID, password, and node name.
5. The ESP32 reboots onto your home Wi-Fi.

Hold the ESP32 `BOOT` button during startup to clear saved Wi-Fi settings.

Starter pins:

| Function | GPIO |
| --- | --- |
| RF TX | 26 |
| RF RX | 27 |
| IR TX | 4 |
| IR RX | 14 |

After Wi-Fi setup, open Serial Monitor and copy the ESP32 IP. Add that IP as a node in the dashboard, for example:

```text
http://192.168.1.42
```

## ESP32 HTTP API

Health:

```http
GET /health
```

Send RF:

```http
GET /send/rf?code=123456&bits=24&protocol=1&pulse_length=350&repeat=6
```

Send raw IR:

```http
POST /send/ir/raw?khz=38&repeat=1
Content-Type: text/plain

9000,4500,560,560,560,1690
```

Capture RF:

```http
GET /capture/rf?timeout_ms=8000
```

Capture IR:

```http
GET /capture/ir?timeout_ms=10000
```

## Signal notes

- Fanzo MINI remote is probably RF, but confirm by capture. If no RF capture appears, check frequency and antenna length.
- Cheap RF modules work best with fixed-code 433 MHz remotes. Rolling-code remotes will not clone cleanly.
- AC remotes usually send full-state IR frames. Capture each complete AC state you care about, such as `Cool 24 Fan Auto`.
- App state can drift when someone uses the original physical remote.

## Network notes

This is local-first software. Run it only on trusted home Wi-Fi. Do not expose the laptop port to the internet.
