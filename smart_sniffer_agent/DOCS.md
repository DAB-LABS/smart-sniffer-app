![SMART Sniffer App for Home Assistant OS](https://raw.githubusercontent.com/DAB-LABS/smart-sniffer-app/main/smart_sniffer_agent/SMARTsniffer_app.png)

## Your Drives' Health, On Its Own Page

The app shows your drives' health on its own page. The integration turns it into entities, alerts and history.

The app reads SMART data from the drives in your Home Assistant machine (the boot SSD or NVMe that HAOS runs on, and any other drive it can see) and shows it in the **Control Center**, the app's own page. It can also show the drives on your other machines that run the SMART Sniffer agent, and replicas: fake drives for testing Home Assistant.

The page is complete on its own. The **SMART Sniffer integration** is optional: it adds entities, history, notifications and automations in Home Assistant, and the page can add this machine, your other machines and your replicas to it with one click.

![Agent Control Center](https://raw.githubusercontent.com/DAB-LABS/smart-sniffer-app/main/smart_sniffer_agent/agent_control_center.png)

## How SMART Sniffer Works

![SMART Sniffer Architecture](https://raw.githubusercontent.com/DAB-LABS/smart-sniffer-app/main/smart_sniffer_agent/smartsniffer-architecture.png)

SMART Sniffer has three parts:

- **The App** (this): runs the SMART Sniffer agent on your Home Assistant machine, the Control Center page, and a replica agent that serves the replicas.
- **The Agent**: the same agent, installed on your other machines (a NAS, a Proxmox host, a workstation or a Mac). The app calls these field agents. See the [SMART Sniffer documentation](https://github.com/DAB-LABS/smart-sniffer) to install it.
- **The Integration**: turns drive data into Home Assistant entities, sensors and alerts. Optional for the app.

The verdict on the page (Attention, Maybe, Health OK) is the integration's own: the app is built with the integration's rule modules, so the page and Home Assistant always agree. The line under the Case file names the integration release the rules came from.

## Getting Started

1. **Start this app.** It begins reading your Home Assistant machine's drives at once.
2. **Open the Control Center** with **Open Web UI**, or turn on **Show in sidebar** on the app's Info page (see [Where it lives](#where-it-lives)). The **Case file** shows one card per drive.
3. If a drive shows **Health not known**, or the app's log says **DRIVE ACCESS BLOCKED**, turn off **Protection Mode**: go to **Settings → Apps → SMART Sniffer → Protection mode**, switch it **OFF**, then restart the app (see [Protection Mode](#protection-mode) below).
4. **Optional: add the integration.** Install the SMART Sniffer integration through HACS. A small exit glyph then appears after each drive's name on the page: grey means Home Assistant does not have this drive yet, and clicking it adds this machine's drives; blue opens the drive's device page in Home Assistant. Home Assistant may also discover the app's agent on its own and offer it under **Settings → Devices & Services**.
5. If you prefer to add it by hand: **Settings → Devices & Services → Add Integration → SMART Sniffer**, then enter the host and port from the app's startup log (`Integration host:`), for example `0449a086-smart-sniffer-agent` and port `9099`.

## The Control Center

The page has three sections.

- **Case file**: every drive this app's own agent reports, in the agent's order. Each card shows the header word, the temperature, power-on hours and power cycles, the counters that matter for that kind of drive, Data Written and Data Read, and the **Field report**, every attribute or NVMe log field as smartctl reports it.
- **Field agents**: the agents on your other machines that you added, one card per machine, with a row per drive and ZFS pool.
- **Replicas**: fake drives and pools, for testing Home Assistant.

Each card's header line carries one of four words:

| Word | Meaning |
|---|---|
| **Attention** (red) | A counter the integration watches says the drive or pool needs attention. **Health FAILED** follows it when the drive's own SMART health check has failed. |
| **Maybe** (gold) | An early warning, such as a few command timeouts or low NVMe spare. |
| **Health OK** (green) | Nothing to report. |
| **Health not known** (grey) | The drive gives no usable SMART data (virtual disks and some USB bridges do this), or the agent is not answering. |

The rows carry the detail once: the row that caused the word is marked in the gutter, red for Attention and a hollow gold square for Maybe, with what was expected written after the value. A row the drive does not report reads "not known".

The page reads this machine's drives and the replicas every 8 seconds while it is open, and your field agents when it opens, when you press **Refresh**, and every 15 minutes while it stays visible.

## Field Agents

A field agent is another machine running the SMART Sniffer agent. Add them with the **+** card, the last card in the Field agents section.

**Nothing on your network is scanned until you ask.** Opening the + card starts a scan of the block your Home Assistant machine is on (for example `10.0.10.0/24`, taken from the Supervisor's network information) on port 9099. You can change the block and the port, and press **Scan** again. Blocks wider than /22 (1,022 addresses) are not scanned; the card offers your own /24 instead. For each address the scan asks only `GET /api/health` and `GET /api/drives`, with no password. The agents that answer are listed with their name, address, version and drive count; tick the ones you want.

**Add by hand.** Type a host name or address and a port and press **Check** to add a machine the scan cannot reach, such as one on another subnet.

Press **Add** to add everything ticked. The app stores field agents in its own options (the **Field Agents** option in the Configuration tab), so they are part of your Home Assistant backups. Adding, hiding or saving a password does not restart the app.

**Passwords.** If the agent on that machine has a token set, its card asks for a password after you add it. The password is the `token:` line in the agent's config file on that machine. The app checks it with the agent before saving it, and never shows it again; the card shows a lock instead. Click the lock to enter a new one. Passwords are stored like the app's own Bearer Token option: in the app's options, masked in the Configuration tab.

**When a machine stops answering**, its card says so and keeps showing the rows from the last good read, with the time it was last seen.

**Hiding cards.** The eye at the foot of a card hides it. The eye in the section header shows how many are hidden; press it to see them again, faded, and click a card's eye to bring it back.

**Removing a field agent.** Remove it from the **Field Agents** list in the app's Configuration tab. Removing it there does not remove it from Home Assistant; do that under **Settings → Devices & Services → SMART Sniffer**.

ZFS pools appear as rows on the card. The agent reports a pool's state and the disks with problems; replicas also show the pool's layout and every disk.

## Replicas

Replicas are fake drives and pools with SMART data you control. They look like real drives to Home Assistant, so you can see how the integration's sensors, notifications and automations react to a failing drive without having one. Nothing in the Replicas section touches a real drive.

**Adding replicas.** The + card lists the replica files under **Replicas**. Six ship with the app: a healthy SATA hard drive, one with reallocated sectors growing, one with uncorrectable errors reported in Device Statistics, an NVMe drive with wear, a degraded ZFS pool and a drive whose SMART health has failed. Tick the ones you want and press **Add**.

**Scenarios.** Each replica card ends in a scenario bar: pick a scenario (for example Command timeouts, NVMe wear or Pool vanished) and press **Apply**. On a replica, the Raw column of the Field report can be edited too.

**Your replica folder.** Your own replica files live in `/share/smart-sniffer/replicas`, so they survive app updates. Files you copy there (for example over the Samba share or SSH apps) appear in the + card the next time it opens. **Upload a replica** in the + card puts a `.json` replica file there for you; a replica file is at most 1 MB.

**Save as replica.** A real drive's card (and an opened drive row on a field agent card) has **Save as replica**. It writes the drive's current readings to a new file in your replica folder and adds it as a replica card. In the copy:

- the serial number is replaced with `REPLICA-` and eight random characters, everywhere it appears;
- the WWN, the NVMe IEEE OUI identifier and each NVMe namespace's EUI-64 and NGUID are replaced with fake values;
- the smartctl command line and the drive's local time are dropped;
- the model, firmware and every counter stay as they are, and the file records the verdict the drive had when it was saved.

Drives with no usable SMART data cannot be saved.

**Download** saves a replica, with its current values, as a file you can keep or upload again. **Remove** takes the replica off the page; it does not touch Home Assistant.

**Replicas in Home Assistant.** All replicas are served by one replica agent inside the app, so they share one integration entry. The first time you add a replica to Home Assistant (with the grey exit glyph on its card), every replica on the page comes with it; later replicas appear at the integration's next poll. The replica agent is not exposed on your network and does not advertise itself, so Home Assistant never discovers it on its own.

## Home Assistant

With the integration installed, an exit glyph follows the title of each card:

- **No glyph**: the integration is not installed, or Home Assistant cannot be reached from the app.
- **Grey**: Home Assistant does not have this machine, field agent or replica yet. Click it and confirm to add it. A field agent is added with its saved password as the token.
- **Blue**: in Home Assistant. On a field agent card it opens the agent's entry in the integration; on a drive or pool it opens the device page.

Adding from the page needs the app's Home Assistant API access, which it has by default.

## Configuration

| Option | Default | Description |
|---|---|---|
| Agent Port | `9099` | Port the agent listens on |
| Bearer Token | _(empty)_ | Optional token for this machine's agent. Must match the token in the integration's entry for it. |
| Scan Interval | `60` | How often the agent refreshes SMART data (seconds) |
| Replica Agent Port | `9100` | Port of the replica agent inside the app. Not exposed on your network. |
| Field Agents | _(empty)_ | Other machines running the SMART Sniffer agent. Added from the Control Center. |

Changing the Agent Port, Bearer Token or Scan Interval needs an app restart, as before. The Control Center reads the Field Agents list each time it loads it, so changes to that list need no restart.

## Where It Lives

**Sidebar.** On the app's Info page, turn on **Show in sidebar**. The Control Center then appears in the sidebar as **SMART Sniffer**.

**Dashboard.** A Webpage card can show the Control Center on a dashboard. Its address is the app's ingress address, `/api/hassio_ingress/<token>/`, which stays the same for an install. To find it, open the Control Center from the sidebar and open the page's frame on its own: in Firefox, right-click inside the page and choose **This Frame → Open Frame in New Tab**; in Chrome or Edge, open the developer tools and look for the `iframe` whose address starts with `/api/hassio_ingress/`. The ingress session expires after a while, and the card then stays blank until the dashboard is reloaded.

## Upgrading from 0.2.x

The Test Lab and its **Enable Mock Test Drives** option are gone. The old setting stays in the app's options after the update until the app starts; the app then removes it, and its log says so once. Drives you had in the Test Lab come back as replicas, and an integration entry you had for the Test Lab now serves the replicas, so nothing needs to be added again.

## Security & Permissions

SMART monitoring needs direct hardware access, the same low-level access that `smartmontools`, `hdparm` and Scrutiny need.

| Permission | Purpose |
|---|---|
| `SYS_RAWIO` | Send SCSI commands to SATA and SAS drives |
| `SYS_ADMIN` | Send admin commands to NVMe drives |
| `full_access` | Open drive device nodes (`/dev/sda`, `/dev/nvme0`) |
| `hassio_api` | Network information for the scan's prefilled block, and the app's own options, where field agents are stored |
| `homeassistant_api` | Adds this app's agents, replicas and field agents to the integration, and finds their device pages |
| `share` | Your replica folder, `/share/smart-sniffer/replicas` |
| Protection Mode OFF | May be needed for the drive permissions above to take effect |

What the app does not do:

- It does not write to your drives.
- It does not contact other machines on your network unless you ask: a scan or a Check from the + card, or a read of the field agents you added.
- It does not send data outside your network.
- Outside its own container, the only files it uses are those in your replica folder.

## Protection Mode

The app uses `smartctl` to send raw SCSI and NVMe commands to your physical drives. This is the only way to read SMART health data.

On some hardware, Home Assistant's Protection Mode stops the container from opening drive device nodes. The app can then see your drive but cannot read its SMART data: the log says "DRIVE ACCESS BLOCKED" and the drive shows **Health not known**.

**If this affects you:** go to **Settings → Apps → SMART Sniffer**, turn OFF **Protection mode**, and restart the app.

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

**The scan finds nothing.** Check that the agent runs on that machine and listens on the port you scanned, and that no firewall blocks it. Machines on another subnet are not in the scanned block: use **Add by hand**.

**"That password was refused".** The password is the `token:` line in the agent's config file on that machine. Check it there and try again.

**"Your replica folder is not available".** The app could not create or write `/share/smart-sniffer/replicas`. Shipped replicas still work; saving and uploading are off until the folder is there.

**Port conflicts.** If port 9099 is already in use, change the Agent Port in the configuration and update the integration to match.

**USB drives showing Health not known.** Many USB enclosures block SMART passthrough in the bridge chip. This is a limit of the enclosure, not the app.

## Links

- [SMART Sniffer Integration & Documentation](https://github.com/DAB-LABS/smart-sniffer)
- [Report an Issue](https://github.com/DAB-LABS/smart-sniffer-app/issues)
