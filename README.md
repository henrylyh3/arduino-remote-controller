# Arduino Remote Controller

Local smart-home controller for RF fans and IR air conditioners.

Flow:

```text
phone/browser -> laptop web app -> HTTP over Wi-Fi -> ESP32 room node -> RF/IR -> appliance
```

## What is included

- FastAPI backend served from the always-on laptop.
- MongoDB Atlas storage for nodes, learned signals, timers, workflows, schedules, and event counts.
- Browser dashboard for phones/laptops on the same Wi-Fi.
- ESP32 starter firmware for 433 MHz RF send/capture and raw IR send/capture.

This starter uses HTTP between the laptop and ESP32 nodes. MQTT can be added later if you want broker/pub-sub behavior, but it is not required for a single local controller.

## Hardware ingredients

Buy one set per room node.

Selected hardware list:

| Ingredient | Shopee link | Confirmed item / variation | Use in this project | Status |
| --- | --- | --- | --- | --- |
| ESP32 DevKit | [ESP32 30 Pin ESP-WROOM-32 Wi-Fi Bluetooth Development Board](https://my.shp.ee/RNU8PSQR) | Type-C ESP32-WROOM-32, 30-pin; CH340C, CP2102, or CH9102 USB chip is OK | Main room node controller. Runs Wi-Fi, HTTP API, RF send/capture, and IR send/capture firmware. | Received |
| 433 MHz RF transmitter + receiver | [SYN115/SYN480R 433MHz transmitter receiver pair](https://shopee.com.my/1Set-2Pcs-433MHZ-Wireless-Transmitter-Receiver-Board-Module-SYN115-SYN480R-ASK-OOK-Chip-PCB-for-arduino-i.72422724.21780766516) | SYN115 transmitter + SYN480R receiver, 433MHz | RF TX sends Fanzo/RF fan commands. RF RX captures/learns the original remote signal. | Pending |
| NPN transistor | [Transistor 2N Series](https://my.shp.ee/Lde77knv) | 2N3904; do not use PNP parts such as 2N3906 or 2N2907 | Drives the IR LED with more current than ESP32 GPIO can safely supply. Needed for stronger AC IR transmit range. | Received |
| IR receiver + small IR transmitter module | [HX1838 Infrared IR Receiver Sensor And LED Transmitter Module](https://my.shp.ee/7tuaENzw) | HX1838 receiver + LED transmitter module | IR receiver learns/captures AC remote signals. Small transmitter module can be used for short-range testing. | Received |
| IR emitter LED pack | [10pcs F3mm F5mm LED Infrared Emitting Diode](https://my.shp.ee/e6HjUNmi) | 5 mm IR emitter 940 nm; choose emitter, not receiver/photodiode/phototransistor | Final AC IR transmitter LED. Connect through 100 ohm resistor and 2N3904 transistor driver. | Pending |
| 1k resistor | [10pcs/pk Resistor 1/4W 1ohm to 1m ohm](https://shopee.com.my/10pcs-pk-Resistor-1-4W-1ohm-10ohm-100ohm-1k-ohm-10k-ohm-100k-ohm-1m-ohm-10m-ohm-5-Fixed-Resistor-i.20221256.7267802426) | 1k | ESP32 GPIO4 to 2N3904 base. | Pending |
| 100 ohm resistor | [10pcs/pk Resistor 1/4W 1ohm to 1m ohm](https://shopee.com.my/10pcs-pk-Resistor-1-4W-1ohm-10ohm-100ohm-1k-ohm-10k-ohm-100k-ohm-1m-ohm-10m-ohm-5-Fixed-Resistor-i.20221256.7267802426) | 100 ohm | IR LED current limit. | Received |
| Jumper wires | [Male to Male 40pcs Dupont Jumper Wire](https://shopee.com.my/Male-to-Male-%28MM%29-40pcs-Dupont-Jumper-Wire-DIY-Experiment-Breadboard-Rainbow-40p-Wires-Cable-10cm-20cm-30cm-for-Arduino-i.1389163043.48700856188) | Male-to-male, 10cm, 40pcs | Breadboard wiring between ESP32, RF modules, IR receiver, and IR driver. This setup uses 14 male-to-male jumpers minimum. | Pending |
| Breadboard | [Mini 400 Points Solderless Prototype Breadboard](https://shopee.com.my/Mini-400-Points-Solderless-Prototype-Breadboard-for-Experiments-Projects-Papan-Tampa-Pematerian--i.53171392.3741813404) | 400 holes | Mounts ESP32, RF TX/RX, IR receiver, IR LED, transistor, and resistors with no loose modules. Compact build; cramped but workable. | Received |

If you use a ready-made IR transmitter module, connect its signal pin to GPIO4, VCC to 3.3V first, and GND to GND. For reliable 2m or longer AC control, use the 5 mm 940 nm IR LED plus transistor circuit below.

## Breadboard and jumper plan

Use a 400-hole MB102 breadboard for the compact build. It is enough for the ESP32 and all modules mounted on the same breadboard with no loose modules, but the layout will be cramped. Use an 830-hole breadboard only if you want easier debugging and more space between parts.

Assumption: ESP32 and modules have male header pins and plug directly into the breadboard. If any module has no header pins, solder male header pins to it first.

Minimum jumper count:

| Jumper type | Minimum count | Recommended purchase | Purpose |
| --- | --- | --- | --- |
| Male-to-male | 14 | 40pcs pack, 10cm | All breadboard wiring. |
| Male-to-female | 0 | Not needed | Only needed if a module is not plugged into the breadboard. |
| Female-to-female | 0 | Not needed | Only needed for direct module-to-module pin wiring without breadboard. |

Male-to-male count breakdown:

| Wiring group | Count |
| --- | --- |
| ESP32 3V3 to breadboard 3V3 rail | 1 |
| ESP32 GND to breadboard GND rail | 1 |
| ESP32 5V/VIN to breadboard 5V rail for IR LED driver | 1 |
| SYN115 VCC/GND to rails | 2 |
| SYN115 DATA to GPIO26 | 1 |
| SYN480R VCC/GND to rails | 2 |
| SYN480R DATA to GPIO27 | 1 |
| HX1838 VCC/GND to rails | 2 |
| HX1838 OUT to GPIO14 | 1 |
| GPIO4 to 1k resistor / 2N3904 base node | 1 |
| 2N3904 emitter to GND rail | 1 |

The 1k resistor, 100 ohm resistor, 2N3904, and 5 mm IR LED plug directly into breadboard holes, so they do not need female jumper wires.

Final compact breadboard layout reference:

![Final purchased breadboard layout](final-purchased-breadboard-layout.png)

Use the image for physical placement. Use the connection summary below for exact GPIO wiring.

## Circuit diagram

![ESP32 RF and IR room node circuit diagram](circuit-diagram.svg)

Connection summary:

| Part | Pin | Connect to |
| --- | --- | --- |
| SYN115 RF transmitter | DATA | ESP32 GPIO26 |
| SYN115 RF transmitter | VCC | ESP32 3V3 |
| SYN115 RF transmitter | GND | ESP32 GND |
| SYN480R RF receiver | DATA | ESP32 GPIO27 |
| SYN480R RF receiver | VCC | ESP32 3V3 |
| SYN480R RF receiver | GND | ESP32 GND |
| IR receiver | OUT/SIGNAL | ESP32 GPIO14 |
| IR receiver | VCC | ESP32 3V3 |
| IR receiver | GND | ESP32 GND |
| IR LED driver | ESP32 GPIO4 | 1k resistor to 2N3904 base |
| IR LED driver | 2N3904 emitter | ESP32 GND |
| IR LED driver | 2N3904 collector | IR LED short leg |
| IR LED driver | IR LED long leg | 100 ohm resistor to ESP32 5V/VIN |

Important: keep the SYN115 transmitter and SYN480R receiver powered from ESP32 3V3. Do not power the RF receiver from 5V when DATA is connected directly to GPIO27.

## Laptop setup

### Standalone executable

No Python installation or terminal command is needed to run a built executable.

On macOS, double-click:

```text
backend/dist/Smart Home Controller.app
```

The app opens the dashboard in a native window and starts the server on port `8000`. Keep that window open while phones or ESP32 nodes use it. Closing the window or choosing `Quit` stops the server; it does not continue running invisibly in the background. The macOS build runs on Apple Silicon Macs; build again on an Intel Mac for an Intel-compatible app.

The executable reads its MongoDB settings from:

```text
macOS:  ~/Library/Application Support/Smart Home Controller/config.env
Windows: %LOCALAPPDATA%\Smart Home Controller\config.env
```

The file format is:

```dotenv
MONGO_URI=mongodb+srv://USERNAME:PASSWORD@YOUR_CLUSTER.mongodb.net/
MONGO_DB=smart_controller
APP_TIMEZONE=Asia/Kuala_Lumpur
```

The executable will show an error and stop when `MONGO_URI` is missing or Atlas cannot be reached.

The same configuration is intentionally published at `backend/static/config.env` and is available from the running app at `/static/config.env`. This public copy includes the MongoDB credentials and is also bundled into standalone executable builds.

Build a fresh macOS application after changing the backend or frontend:

```bash
cd backend
./build_executable.sh
```

Windows executables must be built on Windows. Run `backend\\build_executable.bat`; the result is `backend\\dist\\Smart Home Controller.exe`. Python is required only on the computer performing the build, not on computers running the resulting executable.

### Development server

Create `backend/.env` from `backend/.env.example` and add the real Atlas URI. The `.env` file is ignored by Git.

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

MongoDB settings may also be supplied as environment variables:

```bash
MONGO_URI='mongodb+srv://USERNAME:PASSWORD@YOUR_CLUSTER.mongodb.net/' \
MONGO_DB=smart_controller \
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

The `smart_controller` database uses separate collections:

| Collection | Content |
| --- | --- |
| `nodes` | ESP32 node names, addresses, order, and health metadata |
| `devices` | Signal ownership links between nodes and saved signals |
| `signals` | Learned RF and IR payloads |
| `ac_controllers` | Virtual AC controller state |
| `timers`, `timer_presets` | One-time and reusable timers |
| `schedules`, `workflow_schedules` | Recurring schedules |
| `workflows`, `workflow_steps` | Workflow definitions |
| `workflow_runs`, `workflow_run_steps` | Active and historical workflow execution |
| `events` | Event log and signal counters |
| `_counters` | Internal numeric ID allocation |

The existing SQLite data can be imported once with:

```bash
cd backend
source .venv/bin/activate
python migrate_sqlite_to_mongo.py smart_home.sqlite3
```

The script preserves record IDs and refuses to overwrite populated MongoDB collections unless `--replace` is supplied. SQLite is not used by the running application after migration.

For an older installation that stored every entity in one MongoDB `esp32` collection, split and verify it once with:

```bash
cd backend
source .venv/bin/activate
python migrate_mongo_collections.py --drop-source
```

Default schedule timezone is `Asia/Kuala_Lumpur`. Override with:

```bash
APP_TIMEZONE=Asia/Kuala_Lumpur uvicorn app.main:app --host 0.0.0.0 --port 8000
```

## Timers and workflows

The dashboard has three tabs:

- `Control`: run saved signals or workflows, add timers, add schedules, and view active jobs.
- `Configuration`: add nodes, learn/rename/delete signals, and create/edit/delete workflows.
- `Log`: view controller events newest-first, 10 per page.

Each air-conditioner controller has a name-based page, such as
`http://127.0.0.1:8000/nicole`. From another device, replace `127.0.0.1` with the
host laptop's local IP address. Controller names appear as navigation tabs and can
be renamed from their controller page. Dashboard controller tiles continue to open the popup.

Air-conditioner controllers are global and store their brand and protocol separately
from room nodes. Choose the destination node in the controller before sending; each
controller remembers the last node that successfully received its command.
Supported protocol values are `daikin64` (Daikin) and `panasonic_ac` (Panasonic
216-bit full-state). Commands are encoded by the selected controller protocol; adding
a Panasonic controller does not change existing Daikin controllers. The controller
remembers its last sent power, temperature, fan, and swing state. Physical remote use
can still make that remembered state drift from the appliance.

Signals and workflows are both actions. The same timer and schedule forms can target either one; only workflows contain multiple steps.
The node list under `Configuration` supports renaming, guarded deletion, and desktop drag-and-drop ordering. The drag grip is hidden on mobile. Node order is shared by Configuration and Control signal tabs.
Node health is checked by one lightweight backend loop every 15 seconds. Enabled nodes show green when their `/health` endpoint responds, red after a failed check, and grey before the first check or when disabled. Results are cached in memory, so opening more dashboards does not create more ESP32 health requests. Offline, disabled, and not-yet-checked node tabs remain visible under `Control`, but cannot be selected or used to send signals.
Star signals and workflows under `Configuration` to choose which action tiles appear under `Control`. Starred workflows appear first; starred signals are grouped into node tabs below them. Configuration signals are also grouped by node. Timer and schedule selectors still include all configured actions.
Desktop timer delays are entered in minutes and accept positive decimals. On mobile, use the native minute and second selectors. Timer names are generated from the selected action. Workflow minute/hour delays also accept decimals; for example, `0.1` minute is stored as `6` seconds. Use `0` on a workflow step for no delay.

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
Saved workflows can be edited from the `Configuration` tab. Edits affect future runs, not runs already created.

Schedules can run either a signal or a workflow:

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

When capture succeeds, the learned signal is saved as a dashboard action automatically. Name the signal with the device/action you want to recognise later, such as `Bedroom AC Cool 24` or `Living fan speed 2`. Rename or delete it under `Configuration` -> `Signals`. A signal used by a workflow cannot be deleted until it is removed from that workflow.

If the same signal is captured again on the same ESP32 node, the dashboard shows the existing saved signal name and does not create a duplicate button. RF duplicates are matched by code, bit length, and protocol. IR raw duplicates allow small timing differences between captures.
IR is the default signal type in the Learn signal form. While capture is running, use `Stop capture` to cancel it. Upload the current firmware so the ESP32 also exits its capture loop immediately when the backend closes the request.

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

After Wi-Fi setup, open Serial Monitor and copy the ESP32 IP. Add that IP as a node in the dashboard; `http://` is added automatically. For example:

```text
192.168.1.42
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

Device control traffic remains inside the home network, but MongoDB Atlas persistence requires internet access. If Atlas is unavailable, the app cannot load or save controller data. Add the host network's public IP to the Atlas Network Access list and do not use `0.0.0.0/0` unless you accept public connection attempts. Run the web app only on trusted home Wi-Fi and do not port-forward its HTTP port.
