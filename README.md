![SMART Sniffer App for Home Assistant OS](https://raw.githubusercontent.com/DAB-LABS/smart-sniffer-app/main/smart_sniffer_agent/SMARTsniffer_app.png)

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![GitHub release](https://img.shields.io/github/v/release/DAB-LABS/smart-sniffer-app)](https://github.com/DAB-LABS/smart-sniffer-app/releases)
[![HA App](https://img.shields.io/badge/Home%20Assistant-App-41BDF5?logo=homeassistant)](https://www.home-assistant.io/)

## Monitor Your Home Assistant Drive

This app monitors the health of the drive inside your Home Assistant machine — the boot SSD or NVMe that HAOS runs on. It reads SMART data directly from the hardware and reports it to the **SMART Sniffer integration** as Home Assistant entities: temperature, health status, power-on hours, and more.

No SSH access, no scripts, no external tools. Install the app, install the integration, and your system drive is monitored.

## Why a Separate Repo?

SMART Sniffer has two parts that serve different purposes:

- **This repo (smart-sniffer-app)** — The HA app that runs a lightweight agent on your HAOS machine. It reads SMART data from the local system drive and includes a Test Lab with simulated drives for development and testing.
- **[smart-sniffer](https://github.com/DAB-LABS/smart-sniffer)** — The HA integration and standalone agent. The integration turns drive data into HA entities, sensors, and alerts. The standalone agent runs on remote machines (NAS, Proxmox host, workstation, Mac) to monitor their drives over the network.

The app handles your local HAOS drive. For everything else, see the [integration repo](https://github.com/DAB-LABS/smart-sniffer).

## Installation

### Step 1: Add the Repository

1. In Home Assistant, go to **Settings → Add-ons → Add-on Store**
2. Click the **⋮** menu (top right) → **Repositories**
3. Add this URL:

```
https://github.com/DAB-LABS/smart-sniffer-app
```

4. Click **Add**, then close the dialog

### Step 2: Install the App

1. Find **SMART Sniffer App** in the add-on store (you may need to refresh)
2. Click **Install**
3. Once installed, click **Start**

### Step 3: Connect the Integration

1. Install the **SMART Sniffer integration** via [HACS](https://github.com/DAB-LABS/smart-sniffer) if you haven't already
2. Your HAOS drive should be **auto-discovered** — look for a notification under **Settings → Devices & Services** prompting you to set up SMART Sniffer
3. If auto-discovery doesn't appear, add it manually: **Settings → Devices & Services → Add Integration → SMART Sniffer**, then enter host `172.30.33.1` and port `9099`
4. Your system drive will appear as a device with sensors for temperature, health, attention state, and SMART attributes

## How It Works

![SMART Sniffer Architecture](https://raw.githubusercontent.com/DAB-LABS/smart-sniffer-app/main/smart_sniffer_agent/smartsniffer-architecture.png)

The app runs `smartctl` inside the HA container to read SMART attributes from the host drive. It exposes the data via a local HTTP API. The SMART Sniffer integration polls this API and creates HA entities — sensors, binary sensors, and diagnostic attributes — that you can use in dashboards, automations, and alerts.

The agent also advertises itself via mDNS (Zeroconf), so the integration can discover it automatically on most networks.

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
2. Enter host `172.30.33.1` and port `9100`

This creates a second connection alongside your real drive — mock drives and real drives are managed independently.

## Web UI

Click **Open Web UI** in the app sidebar to access the Agent Control Center — a real-time dashboard showing the status of both the local drive agent and the Test Lab. From here you can see agent health, IP addresses, drive counts, and manage mock drive attributes.

![Agent Control Center](https://raw.githubusercontent.com/DAB-LABS/smart-sniffer-app/main/smart_sniffer_agent/agent_control_center.png)

## Troubleshooting

**No drives detected** — The app needs the `SYS_RAWIO` capability to read SMART data from host drives. This is configured automatically. Check the app logs if drives aren't appearing.

**Port conflicts** — If port 9099 is already in use, change the Agent Port in configuration and update the integration to match.

**USB drives showing UNSUPPORTED** — Many USB enclosures block SMART passthrough at the hardware level. This is a limitation of the USB bridge chip, not the app.

**mDNS not discovered** — If you have multiple HA instances on the same network, mDNS names may collide. Add the agent manually via IP address as described in Step 3 above.

## Links

- [SMART Sniffer Integration & Documentation](https://github.com/DAB-LABS/smart-sniffer)
- [CHANGELOG](CHANGELOG.md)
- [Report an Issue](https://github.com/DAB-LABS/smart-sniffer-app/issues)
