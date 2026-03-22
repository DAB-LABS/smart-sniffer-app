# SMART Sniffer App

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
3. **Add the local agent** in the integration: go to Settings → Devices & Services → SMART Sniffer → Add Device, and enter host `localhost` port `9099`
4. Your system drive will appear as a device with sensors for temperature, health, attention state, and SMART attributes

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

To connect mock drives to Home Assistant, add a second agent in the integration using host `172.30.33.1` and port `9100`.

## Web UI

Click **Open Web UI** to access the Agent Control Center — a real-time dashboard showing the status of both the local drive agent and the Test Lab. From here you can see agent health, drive counts, and manage mock drive attributes.

## Troubleshooting

**No drives detected** — The app needs the `SYS_RAWIO` capability to read SMART data from host drives. This is configured automatically. Check the app logs if drives aren't appearing.

**Port conflicts** — If port 9099 is already in use, change the Agent Port in configuration and update the integration to match.

**USB drives showing UNSUPPORTED** — Many USB enclosures block SMART passthrough at the hardware level. This is a limitation of the USB bridge chip, not the app.

## Links

- [SMART Sniffer Integration & Documentation](https://github.com/DAB-LABS/smart-sniffer)
- [Report an Issue](https://github.com/DAB-LABS/smart-sniffer-app/issues)
