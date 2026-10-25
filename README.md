![SMART Sniffer App for Home Assistant OS](https://raw.githubusercontent.com/DAB-LABS/smart-sniffer-app/main/smart_sniffer_agent/SMARTsniffer_app.png)

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![GitHub release](https://img.shields.io/github/v/release/DAB-LABS/smart-sniffer-app)](https://github.com/DAB-LABS/smart-sniffer-app/releases)
[![HA App](https://img.shields.io/badge/Home%20Assistant-App-41BDF5?logo=homeassistant)](https://www.home-assistant.io/)

> **SMART Sniffer is evolving, for the better.** With integration v0.9 the Control Center moves into the integration, so every machine lives in one place. This App keeps watching the drives of your Home Assistant box and shows them on its own page. Two parts, one place to look. Better together.

## The Drives of This Box

Same Control Center. The App adds the drives of the box Home Assistant runs on. Install the integration for everything else.

This app reads S.M.A.R.T. data from the drives inside your Home Assistant machine (the boot SSD or NVMe that HAOS runs on, and any other drive it can see) and shows them on its own page, **SMART Sniffer App**, read only. No SSH access, no scripts, no external tools.

Your other machines, replicas (test drives), names, thresholds and Agentic access live in the **SMART Sniffer integration's Control Center**, a **SMART Sniffer** page in the sidebar (integration 0.9 and later, from HACS). The integration also turns this box's drives into sensors, history, notifications and automations. A card under the drives on the App's page opens the Control Center.

![Agent Control Center](https://raw.githubusercontent.com/DAB-LABS/smart-sniffer-app/main/smart_sniffer_agent/agent_control_center.png)

> **Got drives on other machines?** If you also run a NAS, Proxmox host, Mac, Linux box or Windows machine, the [SMART Sniffer Agent](https://github.com/DAB-LABS/smart-sniffer) installs directly on those hosts. One small binary per machine, no Docker required. The integration finds them on your network and shows them next to this box in the Control Center.

## Which Component Do I Need?

| Your setup | What to install | Repo |
|---|---|---|
| Drives inside your HAOS or Supervised machine | **This App**, and the **Integration** for sensors, alerts and the Control Center | You're here |
| Drives on a NAS, Proxmox host, Mac, Linux box or Windows machine | The **Agent** on that machine, and the **Integration** | [smart-sniffer](https://github.com/DAB-LABS/smart-sniffer) |
| Home Assistant Container or Core | The **Integration**, and the **Agent** on the Home Assistant machine (apps need the Supervisor) | [smart-sniffer](https://github.com/DAB-LABS/smart-sniffer) |
| Test drives for trying alerts and automations | The **Integration**: + > Replica | [smart-sniffer](https://github.com/DAB-LABS/smart-sniffer) |

**Common combinations:**

- **Bare metal NUC or mini PC running HAOS**: the App and the Integration.
- **HAOS in a VM with drives on the host**: the Agent on the host and the Integration. The App sees only the virtual disk.
- **HAOS on bare metal and a NAS on the network**: the App for the HAOS drive, the Agent on the NAS, and the Integration to see both in one Control Center.

## Installation

### Step 1: Add the Repository

1. In Home Assistant, go to **Settings > Apps > App Store**
2. Click the **three-dot menu** (top right) > **Repositories**
3. Add this URL:

```
https://github.com/DAB-LABS/smart-sniffer-app
```

4. Click **Add**, then close the dialog

### Step 2: Install the App

1. Find **SMART Sniffer App** in the app store (you may need to refresh)
2. Click **Install**
3. Once installed, click **Start**
4. Click **Open Web UI**, or turn on **Show in sidebar** to open the page from the sidebar as **SMART Sniffer App**. It shows one card per drive.
5. If a drive shows **Health not known**, or you see **"DRIVE ACCESS BLOCKED"** in the logs, turn off **Protection Mode**: go to **Settings > Apps > SMART Sniffer App > Protection mode**, switch it **OFF**, then restart the app (see [Security & Permissions](#security--permissions) for details)

### Step 3: Install the Integration

The integration turns drive data into Home Assistant entities, history, notifications and automations, and has the Control Center for all your machines. Use version 0.9 or later.

1. Install [HACS](https://hacs.xyz) if you don't have it
2. In HACS, search for **SMART Sniffer**, download it, and restart Home Assistant
3. Add this box: press **Create sensors and alerts for HAOS** under DRIVES on the App's page, or **Add** on the **SMART Sniffer (HAOS)** card Home Assistant shows under **Settings > Devices & Services**
4. Open the Control Center with **Open** on the card under the drives, or **SMART Sniffer** in the sidebar

To add this box by hand instead: **Settings > Devices & Services > Add Integration > SMART Sniffer**, then the host and port from the app's startup log (`Integration host:`), for example `0449a086-smart-sniffer-agent` and port `9099`.

## How It Works

![SMART Sniffer Architecture](https://raw.githubusercontent.com/DAB-LABS/smart-sniffer-app/main/smart_sniffer_agent/smartsniffer-architecture.png)

The app runs `smartctl` inside its container to read SMART attributes from the host's drives, through the same agent you can install on other machines. Its page reads that agent and judges every drive with the integration's own rules, which are built into the app.

With integration 0.9 or later installed, the app tells Home Assistant where its agent is (host and port only, through the Supervisor's discovery), so Home Assistant can offer it. The integration polls the agent and creates the entities. The agent also advertises itself with mDNS (Zeroconf).

![Startup Summary](images/startup-summary-log.png)

## The App's Page

This box's cards, one per drive (including USB drives mounted under `/media` and the data disk, also after **Move data disk**), per ZFS pool and per filesystem that belongs to no drive or pool, each with its health word (Attention, Maybe, Health OK or Health not known), every row that caused it, its disk space, and Details for the full readings. The cards are read only. Large counts read as sizes and days, with the exact number a click away. Each card can be downloaded as a JSON file.

Under the cards, one card leads on:

- **Integration 0.9 or later**: **Open** goes to the Control Center. Until the integration has its first machine, the card says so, and **Create sensors and alerts for HAOS** under DRIVES adds this box.
- **Integration downloaded but Home Assistant not restarted**: it asks you to restart.
- **Integration 0.8.x**: it asks you to update it, with **Open in HACS**.
- **No integration**: it says how to install it. Close it with the x if you do not want it.

See [DOCS.md](smart_sniffer_agent/DOCS.md) for the details.

## Upgrading from 0.3.x

**Update the integration to 0.9 or later first**, restart Home Assistant, then update the App.

- **Your other machines move into the integration.** The App hands over the machines its 0.3.x page kept (address, port and password), with their names, hidden cards, card order and thresholds, once, and removes each from its options when the integration confirms it. The Control Center shows a line saying what moved.
- **Replicas moved into the integration**, where they work on every Home Assistant install. The App's old Replica machine is removed; create replicas again in the Control Center with **+ > Replica**. Your own files in `/share/smart-sniffer/replicas` stay where they are.
- **The sidebar entry is now "SMART Sniffer App"**, apart from the integration's "SMART Sniffer".

## Configuration

| Option | Default | Description |
|---|---|---|
| Agent Port | `9099` | Port the agent listens on |
| Bearer Token | _(empty)_ | Optional token for this machine's agent. Must match the token in the integration's entry for it. |
| Scan Interval | `60` | How often SMART data is refreshed (seconds) |
| Field Agents | _(empty)_ | Machines App 0.3.x kept, waiting to be handed to the integration. Each is removed once the integration has it. |
| Card Order, Hidden Cards, Card Names, Machine Names | _(empty)_ | What App 0.3.x kept for its cards. A field machine's entries go to the integration with it; this box's stay here. |
| Thresholds | _(empty)_ | Alert thresholds App 0.3.x kept. This box's still judge its cards on the App's page. |

## Security & Permissions

SMART Sniffer needs direct hardware access to read drive health data. This is not optional; it is how SMART monitoring works on Linux. Every tool that reads SMART data (`smartmontools`, `hdparm`, Scrutiny) needs the same access.

**What the app needs and why:**

| Permission | Purpose |
|---|---|
| `SYS_RAWIO` | Send SCSI commands to SATA and SAS drives |
| `SYS_ADMIN` | Send admin commands to NVMe drives |
| `full_access` | Open drive device nodes (`/dev/sda`, `/dev/nvme0`) |
| `hassio_api` | The app's own options, and the discovery message that tells integration 0.9 and later where this box's agent is (host and port only) |
| `homeassistant_api` | Tells which SMART Sniffer integration is installed, adds this box to it, and hands over the machines App 0.3.x kept |
| `discovery` | The `smart_sniffer` discovery service, posted only while integration 0.9 or later is installed |
| `media` | Disk space of the USB drives Home Assistant mounts under `/media`, read-only |
| Protection Mode OFF | May be needed to allow the drive permissions above to take effect |

**What the app does NOT do:**

- Does not write to your drives
- Does not contact other machines on your network
- Does not send data outside your network or phone home
- Outside its own container, reads only the disk space of what Home Assistant mounts under `/media`

**Why you may need to turn off Protection Mode:**

On some hardware, Home Assistant's Protection Mode stops the container from opening drive device nodes. The app can then see that your drive exists but cannot read its SMART data: you'll see a "DRIVE ACCESS BLOCKED" warning in the logs and the drive shows Health not known.

If this affects you, go to **Settings > Apps > SMART Sniffer App**, switch Protection Mode **OFF**, then restart the app.

![Protection Mode toggle](https://raw.githubusercontent.com/DAB-LABS/smart-sniffer-app/main/smart_sniffer_agent/protection_mode_button.png)

After disabling Protection Mode, you'll see this warning. That's expected:

![Protection Mode disabled warning](images/protection-mode-disabled-warning.png)

**Our commitment to transparency:**

SMART Sniffer is fully open source. The Go agent, startup scripts, AppArmor profile and integration code are all published here for anyone to audit. We ship a custom AppArmor security profile that documents exactly what the container accesses. We're committed to requesting only the minimum permissions needed and being upfront about why each one is required.

## Troubleshooting

**Drives show "Health not known" or no SMART data.** Check the app logs for "DRIVE ACCESS BLOCKED". If you see it, turn off Protection Mode (see above) and restart the app.

![Drive Access Blocked log](images/drive-access-blocked-log.png)

**No drives detected.** The app needs `SYS_RAWIO` to read SMART data from host drives. This is configured automatically. Check the app logs if drives aren't appearing.

**My other machines and replicas are gone from the App's page.** They moved to the integration's Control Center: press **Open** on the card under the drives. See [Upgrading from 0.3.x](#upgrading-from-03x).

**Port conflicts.** If port 9099 is already in use, change the Agent Port in configuration and update the integration to match.

**USB drives showing Health not known.** Many USB enclosures block SMART passthrough at the hardware level. This is a limitation of the USB bridge chip, not the app.

**mDNS not discovered.** The app uses your HA hostname (Settings > System > General) to create a unique mDNS name. If you have multiple HA instances, make sure each has a different hostname. If discovery still doesn't work, add the agent with **Create sensors and alerts for HAOS** on the App's page or by hand as described in Step 3 above.

## Links

- [SMART Sniffer Agent & Integration](https://github.com/DAB-LABS/smart-sniffer): standalone agent for remote machines and the HACS integration
- [CHANGELOG](CHANGELOG.md)
- [Report an Issue](https://github.com/DAB-LABS/smart-sniffer-app/issues)
