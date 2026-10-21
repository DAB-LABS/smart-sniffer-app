![SMART Sniffer App for Home Assistant OS](https://raw.githubusercontent.com/DAB-LABS/smart-sniffer-app/main/smart_sniffer_agent/SMARTsniffer_app.png)

> **SMART Sniffer is evolving, for the better.** With integration v0.9 the Control Center moves into the integration, so every machine lives in one place. This App keeps watching the drives of your Home Assistant box and shows them on its own page. Two parts, one place to look. Better together.

## The Drives of This Box

The app reads SMART data from the drives in your Home Assistant machine (the boot SSD or NVMe that HAOS runs on, and any other drive it can see) and shows them on its own page, **SMART Sniffer App**.

Same Control Center. The App adds the drives of the box Home Assistant runs on. Install the integration for everything else.

All your machines live in the **SMART Sniffer integration's Control Center** (integration 0.9 and later, from HACS): the cards of every machine, adding machines, names, thresholds, replicas and Agentic access. The App's page shows this box only, read only, and a card under the drives opens the Control Center.

![Agent Control Center](https://raw.githubusercontent.com/DAB-LABS/smart-sniffer-app/main/smart_sniffer_agent/agent_control_center.png)

## How SMART Sniffer Works

![SMART Sniffer Architecture](https://raw.githubusercontent.com/DAB-LABS/smart-sniffer-app/main/smart_sniffer_agent/smartsniffer-architecture.png)

SMART Sniffer has three parts:

- **The App** (this): runs the SMART Sniffer agent on your Home Assistant machine, and a read-only page of this box's drives.
- **The Agent**: the same agent, installed on your other machines (a NAS, a Proxmox host, a workstation or a Mac). See the [SMART Sniffer documentation](https://github.com/DAB-LABS/smart-sniffer) to install it.
- **The Integration**: turns drive data into Home Assistant entities, sensors and alerts, and has the Control Center, a **SMART Sniffer** page in the sidebar for admins, with every machine on it.

The verdict on the App's page (Attention, Maybe, Health OK) is the integration's own: the app is built with the integration's rule modules. The page header names the integration release the rules came from (**rules**).

## Getting Started

1. **Start this app.** It begins reading your Home Assistant machine's drives at once.
2. **Open its page** with **Open Web UI**, or turn on **Show in sidebar** on the app's Info page (see [Where it lives](#where-it-lives)). It shows one card per drive.
3. If a drive shows **Health not known**, or the app's log says **DRIVE ACCESS BLOCKED**, turn off **Protection Mode**: go to **Settings → Apps → SMART Sniffer App → Protection mode**, switch it **OFF**, then restart the app (see [Protection Mode](#protection-mode) below).
4. **Install the SMART Sniffer integration** (0.9 or later) from HACS and restart Home Assistant. The card under the drives says how.
5. **Add this box to it.** Press **Create sensors and alerts for HAOS** under DRIVES on the App's page, or **Add** on Home Assistant's Discovered card, **SMART Sniffer (HAOS)**, under **Settings → Devices & Services**. Either one creates sensors and alerts for every drive of this box.
6. **Open the Control Center** with **Open** on the card under the drives, or **SMART Sniffer** in the sidebar. Add your other machines and replicas there.

To add this box by hand instead: **Settings → Devices & Services → Add Integration → SMART Sniffer**, then the host and port from the app's startup log (`Integration host:`), for example `0449a086-smart-sniffer-agent` and port `9099`.

## The App's Page

The page shows this box's cards only: one per drive, one per ZFS pool, and one for each filesystem that belongs to no drive or pool, each with the `HAOS` label. A card shows where the drive is, its temperature, power-on hours and data written, its disk space and its health. Large counts read as sizes (28.8 TB) and power-on hours as days (887 d); click one for the exact number and a copy button. The chevron beside the health word opens **Details**: every counter, Data Read, Device Statistics, the disk space in full and every attribute or NVMe log field as smartctl reports it, and **Connect by hand** with the host and port to type into the integration. The arrow at the foot of a card downloads it as a JSON file.

The cards are read only: arranging, hiding, renaming, removing and thresholds are in the integration's Control Center. The line at the top gives this app's agent address and mDNS name, each with a copy button, and the header shows the app version, whether the agent is answering, and the rules version.

Each card's health line carries one of four words:

| Word | Meaning |
|---|---|
| **Attention** (red) | A counter the integration watches says the drive or pool needs attention. **Health FAILED** follows it when the drive's own SMART health check has failed. |
| **Maybe** (gold) | An early warning, such as a few command timeouts or low NVMe spare. |
| **Health OK** (green) | Nothing to report. |
| **Health not known** (grey) | The drive gives no usable SMART data (virtual disks and some USB bridges do this), or the agent is not answering. |

Under the word, a box lists every row that caused it, with what was expected and the value. The page judges with the integration's default limits, plus any thresholds you saved for this box's drives in App 0.3.x. Thresholds you set in the Control Center apply there and to Home Assistant's sensors; this page does not read them.

**Disk space.** Each filesystem on a drive gets a grey bar, up to three per card. Below 80% the bar says how full it is and the size; from 80% the text says how much is free; the number gets heavier at 90% and 95%, and at 95% the bar's outline turns dashed. The agent measures the USB drives Home Assistant mounts under `/media`, and the data disk, shown as `/data`, also after **Move data disk**. Restart the app after plugging in a USB drive.

The page reads this box's drives every 30 seconds while it is open, and every 8 seconds while the agent is not answering.

### The card under the drives

One card across the page, under the drives, says where the rest of SMART Sniffer is:

| Integration | The card |
|---|---|
| 0.9 or later, with this box or another machine set up | **See all your machines in the SMART Sniffer Control Center**, with **Open** |
| 0.9 or later, no machine set up yet | The same title, and that the Control Center opens once SMART Sniffer has its first machine: create this box with the line under DRIVES, or add a machine in Settings, Devices & services |
| 0.9 or later downloaded, Home Assistant not restarted | "Restart Home Assistant to finish the update." |
| 0.9 or later, its page did not start | "The Control Center did not start. Check the Home Assistant log." |
| 0.8.x | **Get more out of SMART Sniffer**: update the integration to 0.9, with **Open in HACS**. Close it with the x |
| Not installed | **Get more out of SMART Sniffer**: install the integration for sensors, alerts and your other machines, with **How to install**. Close it with the x |

A closed card stays closed for every browser. **Create sensors and alerts for HAOS** shows under DRIVES while integration 0.9 or later is installed and this box is not one of its machines.

**Open** goes to the Control Center in the same Home Assistant window. Users who are not admins do not have the Control Center.

## Replicas Moved to the Integration

Replicas, the test drives of App 0.3.x, are now part of the SMART Sniffer integration (0.9 and later), where they work on every Home Assistant install. The App no longer runs a replica agent, and the integration removes the App's old Replica machine and its sensors when Home Assistant starts, with a one-time note. Create them again in the Control Center with **+ > Replica**. Your own files in `/share/smart-sniffer/replicas` stay where they are, and the integration lists them when it can read that folder.

## Moving from 0.3.x

App 0.3.x kept your other machines (field agents) in its own options. With integration 0.9 or later installed, App 0.4.0 hands them over once when it starts: each machine's address, port and password, its name, and the card names, hidden cards, card order and thresholds that go with it. The integration confirms each machine, and the App then removes it, password included, from its options. A machine already in the integration is matched and never added twice. Nothing is sent to an older integration, or without one; the App tries again every 10 minutes while something is left.

The Control Center shows one line about it, for example "Moved 2 machines from the SMART Sniffer App.", with **Show** for any machine that could not be moved and **Discard** to let one go. The App's log says the same. Update the integration first: an App 0.4.0 next to integration 0.8.x keeps your machines in its options until the integration is updated.

This box's own settings stay in the App's options. Its 0.3.x thresholds still judge its cards on this page.

## Configuration

| Option | Default | Description |
|---|---|---|
| Agent Port | `9099` | Port the agent listens on |
| Bearer Token | _(empty)_ | Optional token for this machine's agent. Must match the token in the integration's entry for it. |
| Scan Interval | `60` | How often the agent refreshes SMART data (seconds) |
| Field Agents | _(empty)_ | Machines App 0.3.x kept. Each is removed once the integration has it; any left are waiting to be handed over. Add machines in the Control Center now. |
| Card Order, Hidden Cards, Card Names, Machine Names | _(empty)_ | What App 0.3.x kept for its cards. A field machine's entries go to the integration with it; this box's stay here. Not used by this page. |
| Thresholds | _(empty)_ | Alert thresholds App 0.3.x kept. This box's still judge its cards on this page; a field machine's go to the integration with it. |

Changing the Agent Port, Bearer Token or Scan Interval needs an app restart. The App's page writes none of these options.

## Where It Lives

**Sidebar.** On the app's Info page, turn on **Show in sidebar**. The page then appears in the sidebar as **SMART Sniffer App**, apart from the integration's **SMART Sniffer**.

**Dashboard.** A Webpage card can show the App's page on a dashboard. Its address is the app's ingress address, `/api/hassio_ingress/<token>/`, which stays the same for an install. To find it, open the page from the sidebar and open the page's frame on its own: in Firefox, right-click inside the page and choose **This Frame → Open Frame in New Tab**; in Chrome or Edge, open the developer tools and look for the `iframe` whose address starts with `/api/hassio_ingress/`. If the ingress session expires, the card stays blank until the dashboard is reloaded.

## Upgrading from 0.2.x

The 0.2.x mock drives and their **Enable Mock Test Drives** option are gone. The app removes the old setting from its options when it starts, and its log says so once. For test drives, use **+ > Replica** in the integration's Control Center.

## Security & Permissions

SMART monitoring needs direct hardware access, the same low-level access that `smartmontools`, `hdparm` and Scrutiny need.

| Permission | Purpose |
|---|---|
| `SYS_RAWIO` | Send SCSI commands to SATA and SAS drives |
| `SYS_ADMIN` | Send admin commands to NVMe drives |
| `full_access` | Open drive device nodes (`/dev/sda`, `/dev/nvme0`) |
| `hassio_api` | The app's own options, and the discovery message that tells integration 0.9 and later where this box's agent is (host and port only) |
| `homeassistant_api` | Tells which SMART Sniffer integration is installed, and adds this box to it |
| `discovery` | The `smart_sniffer` discovery service, posted only while integration 0.9 or later is installed |
| `media` | Disk space of the USB drives Home Assistant mounts under `/media`, read-only |
| Protection Mode OFF | May be needed for the drive permissions above to take effect |

What the app does not do:

- It does not write to your drives.
- It does not contact other machines on your network. It talks only to its own agent, the Supervisor and Home Assistant.
- It does not send data outside your network.
- Outside its own container, it reads only the disk space of what Home Assistant mounts under `/media`.

## Protection Mode

The app uses `smartctl` to send raw SCSI and NVMe commands to your physical drives. This is the only way to read SMART health data.

On some hardware, Home Assistant's Protection Mode stops the container from opening drive device nodes. The app can then see your drive but cannot read its SMART data: the log says "DRIVE ACCESS BLOCKED" and the drive shows **Health not known**.

**If this affects you:** go to **Settings → Apps → SMART Sniffer App**, turn OFF **Protection mode**, and restart the app.

![Protection Mode toggle](https://raw.githubusercontent.com/DAB-LABS/smart-sniffer-app/main/smart_sniffer_agent/protection_mode_button.png)

After turning Protection Mode off you will see this warning. That is expected:

![Protection Mode disabled warning](https://raw.githubusercontent.com/DAB-LABS/smart-sniffer-app/main/images/protection-mode-disabled-warning.png)

**What does turning off Protection Mode do?** It lets the app's container open drive device nodes (`/dev/sda`, `/dev/nvme0` and so on) to read them. The app does not write to your drives.

**Is this safe?** SMART Sniffer is open source: the Go agent, the scripts, the AppArmor profile and the integration are all published on GitHub for anyone to review. The app ships a custom AppArmor profile that documents what the container accesses.

- Source code: [github.com/DAB-LABS/smart-sniffer-app](https://github.com/DAB-LABS/smart-sniffer-app)
- Integration: [github.com/DAB-LABS/smart-sniffer](https://github.com/DAB-LABS/smart-sniffer)

## Troubleshooting

**Drives show "Health not known" or no SMART data.** Check the app's log for "DRIVE ACCESS BLOCKED". If you see it, turn off Protection Mode (see above) and restart the app.

![Drive Access Blocked log](https://raw.githubusercontent.com/DAB-LABS/smart-sniffer-app/main/images/drive-access-blocked-log.png)

**"This app's agent is not answering".** The app may still be starting; the page asks again every 8 seconds. If it stays, read the app's log.

**No drives detected.** The app needs the `SYS_RAWIO` capability to read SMART data from host drives. This is configured automatically. Check the app's log if drives are not appearing.

**The card under the drives says to restart Home Assistant.** The integration was updated to 0.9 or later in HACS, and Home Assistant still runs the old one. Restart it.

**My other machines and replicas are gone from this page.** They moved: open the Control Center with **Open** on the card under the drives. The machines App 0.3.x kept are handed over once integration 0.9 or later is installed; replicas are created again there with **+ > Replica**.

**A machine was not moved.** The Control Center's line says why behind **Show**. A machine stays in the App's **Field Agents** option until the integration takes it or you **Discard** it there.

**Port conflicts.** If port 9099 is already in use, change the Agent Port in the configuration and update the integration to match.

**USB drives showing Health not known.** Many USB enclosures block SMART passthrough in the bridge chip. This is a limit of the enclosure, not the app.

## Links

- [SMART Sniffer Integration & Documentation](https://github.com/DAB-LABS/smart-sniffer)
- [Report an Issue](https://github.com/DAB-LABS/smart-sniffer-app/issues)
