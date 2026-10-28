# MeshWarden

**A read-only MeshCore DM bot that keeps an eye on your home — over LoRa, without internet.**

Send `solar` from your MeshCore radio and get your battery state back. Send `status` and see whether your
network, Prometheus, Home Assistant and NAS are alive. Go to bed and get woken up by nothing — unless the
battery runs low, a host goes down or the internet drops; then MeshWarden DMs you.

```
you  -> solar
bot  -> Batt 13.01V ~31% -0.1A | PV 0W | Inv OFF | ok
you  -> today
bot  -> Today PV 1.68kWh peak 358W | Batt min12h 12.76V max 13.68V
bot  -> Morning: night min 12.76V | Batt 13.01V ~31% ... | Net OK | Prom OK | Solar OK
```

## How it works

```
 phone / handheld  ~~ LoRa mesh ~~  repeater  ~~ LoRa ~~  companion node (WiFi firmware)
                                                                 |  TCP :5000 (LAN)
                                                          MeshWarden (Python, any Linux box)
                                                                 |  HTTP (LAN only)
                                          Prometheus · Home Assistant · TrueNAS · gateway
```

- A MeshCore **companion** with the WiFi firmware (e.g. Heltec V4, env `heltec_v4_companion_radio_wifi`)
  sits in your house and exposes the companion protocol on TCP port 5000.
- MeshWarden connects to it with the [`meshcore`](https://pypi.org/project/meshcore/) Python library,
  receives DMs, answers them and pushes alerts.
- Nothing is flashed or changed on the node besides its normal companion firmware.

## Safety by design

- **Read-only.** There are no commands that switch, write or execute anything. Incoming text is only
  ever matched against a fixed list of command words.
- **Allow-list.** Only public keys listed in `ALLOWED_KEYS` get an answer; everybody else is ignored.
  MeshCore DMs are end-to-end encrypted, so a sender key can't simply be spoofed.
- **Rate-limited.** Queue per sender, a small pause between replies and a per-minute cap protect
  airtime and duty cycle (EU 869.4–869.65 MHz: 10 %).
- **Secrets stay local.** Tokens and keys only live in `meshwarden.env` on the bot host
  (`.gitignore`d, `chmod 600`).
- **Watchdog.** Polls the node every 30 s for waiting messages and reconnects if it stops answering.

> ⚠️ The companion's TCP port 5000 has **no authentication**. Anyone on the same LAN can connect to it.
> Put the node into an IoT VLAN and only allow the bot host to reach port 5000.

## Commands

| Command | Answer |
|---|---|
| `solar` / `s` | battery voltage, estimated SoC, current, PV power, inverter, alarms |
| `today` / `t` | PV energy and peak since midnight, battery min (12 h) / max |
| `status` / `st` | internet, Prometheus, solar exporter, Home Assistant, NAS |
| `net` | gateway, internet, DNS, HTTPS round-trip |
| `hosts` | Prometheus targets up/down |
| `sensors` / `temp` | Home Assistant temperature / humidity sensors |
| `printer` | 3D printer status from Home Assistant (Bambu-style entities) |
| `nas` | TrueNAS pools, usage, open alerts |
| `mesh` | your repeater's battery, uptime, noise floor, SNR, rx/tx counters |
| `ping` | pong, hop count and SNR of your message |
| `help` | command list |

Commands are case-insensitive. Long answers are split into up to 3 numbered parts of 140 characters.

## Alerts & morning summary

Checked every 60 s; a problem must persist **5 minutes** before you get a DM, and you get an
"OK again" when it clears:

- battery below 20 % SoC, battery alarm, MPPT error
- Prometheus targets down, Prometheus unreachable, internet down
- NAS pool degraded / NAS API unreachable, Home Assistant unreachable (if configured)
- inverter switched ON/OFF

At `SUMMARY_AT` (default 07:00) you get the night's minimum battery voltage plus `solar` and `status`.

## Requirements

- A MeshCore companion node with WiFi firmware, reachable on your LAN
- Python ≥ 3.10 on any always-on Linux box (Raspberry Pi, NAS VM, home server)
- For `solar` / `today` / alerts: Prometheus scraping a solar exporter that provides
  `battery_voltage`, `battery_current`, `mppt_pv_power`, `inverter_status`,
  `battery_has_alarm`, `mppt_has_error` under one job (`PROM_JOB`).
  The SoC estimate assumes a **12 V LiFePO₄** (4S) battery — adjust `SOC_TABLE` for other chemistries.
- Optional: Home Assistant long-lived token, TrueNAS API key

## Setup

### 1. Companion node with WiFi

Build the MeshCore companion WiFi firmware for your board with your WiFi credentials
(`WIFI_SSID` / `WIFI_PWD` build flags). On Windows, `tools/build-wifi-companion.ps1` (run from your
MeshCore clone) asks for them, builds, flashes, and restores the ini afterwards so no credentials stay on disk.

Give the node a fixed DHCP lease. Since it usually sits close to a repeater, turn TX power down.

### 2. Install the bot

```bash
git clone https://github.com/fsaritome/MeshWarden.git ~/meshwarden
cd ~/meshwarden
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
#   (no venv / no pip on the host? pip install --target lib ... on another machine and copy lib/ over)
cp meshwarden.env.example meshwarden.env && chmod 600 meshwarden.env
nano meshwarden.env          # NODE_HOST, ALLOWED_KEYS, ALERT_KEYS, ...
```

Quick test without the radio:

```bash
set -a; . ./meshwarden.env; set +a
.venv/bin/python -c "import meshwarden as m; print(m.cmd_solar()); print(m.cmd_status()); print(m.cmd_net())"
```

### 3. Run as a service (no root needed)

```bash
mkdir -p ~/.config/systemd/user
cp deploy/meshwarden.service ~/.config/systemd/user/
# using a venv? change ExecStart to %h/meshwarden/.venv/bin/python
systemctl --user daemon-reload
systemctl --user enable --now meshwarden
loginctl enable-linger "$USER"
journalctl --user -u meshwarden -f
```

Update: `git pull && systemctl --user restart meshwarden`.

## Configuration

All settings are environment variables — see [`meshwarden.env.example`](meshwarden.env.example).
Features whose URL or key is empty are simply switched off.

## License

MIT — © 2026 Ferit Sari Tomé. Free to use, modify and share; keep the copyright notice.
See [LICENSE](LICENSE).

Built on [MeshCore](https://github.com/meshcore-dev/MeshCore) and the
[meshcore Python library](https://pypi.org/project/meshcore/).
