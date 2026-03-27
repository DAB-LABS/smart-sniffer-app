![SMART Sniffer App for Home Assistant OS](https://raw.githubusercontent.com/DAB-LABS/smart-sniffer-app/main/smart_sniffer_agent/SMARTsniffer_app.png)

## Monitor Your Home Assistant Drive

This app monitors the health of the drive inside your Home Assistant machine — the boot SSD or NVMe that HAOS runs on. It reads SMART data directly from the hardware and reports it to the **SMART Sniffer integration** as Home Assistant entities: temperature, health status, power-on hours, and more.

No SSH access, no scripts, no external tools. Install the app, install the integration, and your system drive is monitored.

## How SMART Sniffer Works

![SMART Sniffer Architecture](https://raw.githubusercontent.com/DAB-LABS/smart-sniffer-app/main/smart_sniffer_agent/smartsniffer-architecture.png)

SMART Sniffer has two parts:

- **The App** (this) — Runs a lightweight agent on your HA machine that reads SMART data from the local drive. It also includes a Test Lab for experimenting with simulated drives.
- **The Integration** — Connects to agents and turns drive data into HA entities, sensors, and alerts. The integration handles everything you see in Home Assistant.

The app handles your local drive. For monitoring drives on other machines — a NAS, Proxmox host, workstation, or Mac — you install the standalone `smartha-agent` on those machines and point the integration at them. See the [SMART Sniffer integration documentation](https://github.com/DAB-LABS/smart-sniffer) for details on network-based monitoring.

## Getting Started

1. **Install the SMART Sniffer integration** via HACS or manually (if you haven't already)
2. **Start this app** — it begins monitoring your HA system drive immediately
3. If your drive shows **UNSUPPORTED** or you see **"DRIVE ACCESS BLOCKED"** in the logs, turn off **Protection Mode** — Go to **Settings → Apps → SMART Sniffer → Protection mode**, switch it **OFF**, then restart the app (see [Protection Mode](#protection-mode) below)
4. Your HAOS drive should be **auto-discovered** — look for a notification under **Settings → Devices & Services** prompting you to set up SMART Sniffer
5. If auto-discovery doesn't appear, add it manually: **Settings → Devices & Services → Add Integration → SMART Sniffer**, then enter host `0449a086-smart-sniffer-agent` and port `9099`
6. Your system drive will appear as a device with sensors for temperature, health, attention state, and SMART attributes

## Configuration

| Option | Default | Description |
|---|---|---|
| Agent Port | `9099` | Port the agent listens on |
| Bearer Token | _(empty)_ | Optional auth token — must match what you configure in the integration |
| Scan Interval | `60` | How often SMART data is refreshed (seconds) |
| Enable Mock Test Drives | `off` | Launch simulated drives for testing |
| Mock Agent Port | `9100` | Port for the mock test agent |

## Test Lab

The app includes a built-in Test Lab with simulated drives for safely testing the SMART Sniffer integration without touching real hardware. Enable **Mock Test Drives** in the app configuration to activate it.

Mock drives behave exactly like real drives from the integration's perspective. You can adjust their SMART attributes in the web UI and watch how the integration responds — attention states, health changes, temperature alerts, and more.

To connect mock drives to Home Assistant, add them as a **separate agent** in the integration:

1. Go to **Settings → Devices & Services → SMART Sniffer → Add Device**
2. Enter host `0449a086-smart-sniffer-agent` and port `9100`

This creates a second connection alongside your real drive — mock drives and real drives are managed independently.

## Web UI

Click **Open Web UI** to access the Agent Control Center — a real-time dashboard showing the status of both the local drive agent and the Test Lab. From here you can see agent health, drive counts, and manage mock drive attributes.

## Protection Mode

SMART monitoring requires direct hardware access — the same low-level access that `smartmontools`, `hdparm`, and Scrutiny need. The app uses `smartctl` to send raw SCSI and NVMe commands to your physical drive. This is the only way to read SMART health data.

On some hardware, Home Assistant's Protection Mode restricts the container from accessing drive device nodes. When this happens, the app can detect your drive but cannot read its SMART data — you'll see "DRIVE ACCESS BLOCKED" in the logs and your drive will show as UNSUPPORTED.

**If this affects you:** Go to **Settings → Apps → SMART Sniffer**, turn OFF **Protection mode**, and restart the app.

![Protection Mode toggle](https://raw.githubusercontent.com/DAB-LABS/smart-sniffer-app/main/smart_sniffer_agent/protection_mode_button.png)

After disabling Protection Mode, you'll see this warning — that's expected:

![Protection Mode disabled warning](https://raw.githubusercontent.com/DAB-LABS/smart-sniffer-app/main/images/protection-mode-disabled-warning.png)

**What does turning off Protection Mode do?** It allows the app's container to access drive device nodes (`/dev/sda`, `/dev/nvme0`, etc.) for read operations. The app does not write to your drives, does not access your network beyond the local HA instance, and does not send data externally.

**Is this safe?** Yes. SMART Sniffer is fully open source — the Go agent, shell scripts, AppArmor profile, and integration are all published on GitHub for anyone to review. We ship a custom AppArmor security profile that documents exactly what the container accesses. The app reads drive data and serves it over a local API. That's all it does.

- Source code: [github.com/DAB-LABS/smart-sniffer-app](https://github.com/DAB-LABS/smart-sniffer-app)
- Integration: [github.com/DAB-LABS/smart-sniffer](https://github.com/DAB-LABS/smart-sniffer)

## Troubleshooting

**Drives show "UNSUPPORTED" or no SMART data** — Check the app logs for "DRIVE ACCESS BLOCKED". If you see it, turn off Protection Mode (see above) and restart the app.

![Drive Access Blocked log](https://raw.githubusercontent.com/DAB-LABS/smart-sniffer-app/main/images/drive-access-blocked-log.png)

**No drives detected** — The app needs the `SYS_RAWIO` capability to read SMART data from host drives. This is configured automatically. Check the app logs if drives aren't appearing.

**Port conflicts** — If port 9099 is already in use, change the Agent Port in configuration and update the integration to match.

**USB drives showing UNSUPPORTED** — Many USB enclosures block SMART passthrough at the hardware level. This is a limitation of the USB bridge chip, not the app.

## Links

- [SMART Sniffer Integration & Documentation](https://github.com/DAB-LABS/smart-sniffer)
- [Report an Issue](https://github.com/DAB-LABS/smart-sniffer-app/issues)
