![SMART Sniffer App for Home Assistant OS](https://raw.githubusercontent.com/DAB-LABS/smart-sniffer-app/main/smart_sniffer_agent/SMARTsniffer_app.png)

**The drives inside your Home Assistant box, watched.** *Sniff early. Sniff often.*

This App watches the drives of the machine Home Assistant runs on: the boot SSD or NVMe that HAOS lives on, the data disk, and any USB drive Home Assistant mounts. It reads their S.M.A.R.T. data, judges every drive with the same rules as the SMART Sniffer integration, and shows them on its own page, **SMART Sniffer App**.

## Better together

SMART Sniffer comes in two parts.

- **The [SMART Sniffer integration](https://github.com/DAB-LABS/smart-sniffer)** (from HACS) owns every machine. Its **Control Center**, **SMART Sniffer** in the sidebar, shows every drive of every machine, with sensors, history, notifications, automations, replicas for testing and Agentic access.
- **This App** runs the SMART Sniffer agent inside the Home Assistant box, so that box's own drives are watched too.

Install both, and this box shows up in the Control Center as **HAOS**, next to your other machines. Two parts, one place to look.

## Getting started

1. **Start the App.** It begins reading this box's drives at once.
2. **Open its page** with **Open Web UI**, or turn on **Show in sidebar**.
3. **Install the integration** from HACS (search for **SMART Sniffer**, v0.9 or later) and restart Home Assistant.
4. **Add this box**: press **Create sensors and alerts for HAOS** under DRIVES on the App's page, or **Add** on **SMART Sniffer (HAOS)** under **Settings > Devices & services > Discovered**. The App tells Home Assistant where its agent is, so there is nothing to type. This box is never added twice.

If a drive shows **Health not known** and the log says **DRIVE ACCESS BLOCKED**, see [Protection Mode](#protection-mode).

## The App's page

![The SMART Sniffer App page](https://raw.githubusercontent.com/DAB-LABS/smart-sniffer-app/main/images/app-page.png)

The page shows this box only, read only, in the same design as the Control Center: one card per drive, per ZFS pool, and per filesystem that belongs to no drive or pool, each labelled `HAOS`.

- **At a glance**: temperature, days powered on, data written and read, and wear. A value the drive does not report is left out.
- **Disk space** of each mount, including USB drives under `/media` and the data disk (`/data`, also after **Move data disk**). From 80% full the text says how much is free. Restart the App after plugging in a USB drive.
- **Health**, one of four words, with every row that caused it underneath:

| Word | Meaning |
|---|---|
| **Attention** (red) | A counter the integration watches says the drive or pool needs attention. **Health FAILED** follows it when the drive's own SMART health check has failed. |
| **Maybe** (gold) | An early warning, such as a few command timeouts or low NVMe spare. |
| **Health OK** (green) | Nothing to report. |
| **Health not known** (grey) | The drive gives no usable SMART data (virtual disks and some USB bridges), or the agent is not answering. |

- **Details** (the chevron): model, serial, firmware, capacity and interface, every SMART attribute or NVMe log field, the drive's Device Statistics, and **Connect by hand** with this agent's host and port. Click a number or a value for the exact figure and a copy button.
- **Download**: the arrow at the foot of a card saves it as a JSON file.

The line at the top gives this agent's address and mDNS name, with copy buttons. The header shows the App version, whether the agent is answering, and the integration release its rules come from (**rules**). The page refreshes itself every 30 seconds.

Arranging, hiding, renaming, removing cards and editing thresholds happen in the Control Center. This page judges with the integration's default limits, plus any thresholds you saved for this box in App 0.3.x.

### The card under the drives

| Integration | The card |
|---|---|
| v0.9 or later, with at least one machine | **See all your machines in the SMART Sniffer Control Center**, with **Open** |
| v0.9 or later, no machine yet | The same title, and how to add the first machine (this box, with the line under DRIVES) |
| v0.9 or later, Home Assistant not restarted | "Restart Home Assistant to finish the update." |
| 0.8.x | **Get more out of SMART Sniffer**: update the integration, with **Open in HACS** |
| Not installed | **Get more out of SMART Sniffer**: install it for sensors, alerts and your other machines, with **How to install** |

You can close the last two with the x; they stay closed for every browser. The Control Center is for admins only.

## Configuration

| Option | Default | What it does |
|---|---|---|
| Agent Port | `9099` | The port this box's agent listens on |
| Bearer Token | _(empty)_ | Optional password for this box's agent. Set the same one in the integration's entry for this box. |
| Scan Interval | `60` | How often the drives are read, in seconds |

Changing these needs an App restart.

The other options (Field Agents, Card Order, Hidden Cards, Card Names, Machine Names, Thresholds) are what App 0.3.x kept. A field machine's entries move to the integration with it; this box's thresholds still judge its cards on this page.

## Upgrading from 0.3.x

**Update the integration to v0.9 or later first**, restart Home Assistant, then update the App.

- **Your other machines move to the integration** once, with their addresses, passwords, names, hidden cards, card order and thresholds. The integration confirms each one, and the App then removes it from its options. A machine already there is never added twice. The Control Center shows a line saying what moved, with **Show** for anything that could not move.
- **Replicas now live in the integration**, on every install type. The App's old Replica machine is removed; create replicas again with **+ > Replica**. Your files in `/share/smart-sniffer/replicas` stay where they are.
- **The sidebar entry is now "SMART Sniffer App"**, next to the integration's "SMART Sniffer".

Next to integration 0.8.x, the App shows an update card and keeps your machines until you update.

## A dashboard card

A Webpage card can show the App's page on a dashboard. Its address is the App's ingress address, `/api/hassio_ingress/<token>/`, which stays the same for an install. To find it, open the page from the sidebar and open its frame on its own: in Firefox, right-click inside the page and choose **This Frame > Open Frame in New Tab**; in Chrome or Edge, open the developer tools and look for the `iframe` whose address starts with `/api/hassio_ingress/`. If the ingress session expires, the card stays blank until the dashboard is reloaded.

## Security & permissions

Reading SMART data needs direct hardware access. Every SMART tool on Linux needs the same.

| Permission | Why |
|---|---|
| `SYS_RAWIO` | SCSI commands to SATA and SAS drives |
| `SYS_ADMIN` | Admin commands to NVMe drives |
| `full_access` | Opening drive device nodes (`/dev/sda`, `/dev/nvme0`) |
| `hassio_api` | The App's own options, and the discovery message that tells the integration where this box's agent is (host and port only) |
| `homeassistant_api` | Seeing which SMART Sniffer integration is installed, adding this box to it, and handing over the machines App 0.3.x kept |
| `discovery` | The `smart_sniffer` discovery service, posted only while integration v0.9 or later is installed |
| `media` | The disk space of USB drives under `/media`, read only |

The App does not write to your drives, and does not contact other machines on your network or anything outside it. Its page answers only Home Assistant's ingress, and the Bearer Token is never put on a command line.

## Protection Mode

On some hardware, Home Assistant's Protection Mode stops the App from opening drive device nodes. The drive then shows **Health not known**, and the log says **DRIVE ACCESS BLOCKED**.

Go to **Settings > Apps > SMART Sniffer App**, switch **Protection mode** off, and restart the App. Home Assistant then shows a warning; that is expected.

![Protection mode switch](https://raw.githubusercontent.com/DAB-LABS/smart-sniffer-app/main/smart_sniffer_agent/protection_mode_button.png)

## Troubleshooting

**Health not known, or no SMART data.** Look for **DRIVE ACCESS BLOCKED** in the App's log, and turn off Protection Mode if it is there.

**USB drive shows Health not known.** Many USB enclosures do not pass SMART through. That is the enclosure's bridge chip, not the App.

**Home Assistant in a VM.** Inside a VM the App sees only the virtual disk. Install the [agent](https://github.com/DAB-LABS/smart-sniffer#1-install-the-agent) on the host instead.

**My other machines and replicas are gone from the App's page.** They moved to the integration's Control Center. Press **Open** on the card under the drives.

**Port 9099 is taken.** Change Agent Port, then the port in the integration's entry for this box.

**This box is not discovered.** Discovery needs integration v0.9 or later. Otherwise use **Create sensors and alerts for HAOS** on the App's page, or add it by hand: **Settings > Devices & services > Add integration > SMART Sniffer**, with the host and port from the App's log (`Integration host:`). With several Home Assistant instances, give each its own host name (**Settings > System > General**) so their mDNS names differ.

## Links

- [SMART Sniffer integration and agent](https://github.com/DAB-LABS/smart-sniffer)
- [SMART Sniffer App on GitHub](https://github.com/DAB-LABS/smart-sniffer-app)
- [Changelog](https://github.com/DAB-LABS/smart-sniffer-app/blob/main/CHANGELOG.md)
- [Report an issue](https://github.com/DAB-LABS/smart-sniffer-app/issues)
