# Changelog

## 0.4.1 -- 2026-10-23

### Fixed
- **Opening a card's Details no longer freezes the page.** On a page short enough to need no scrollbar, in a browser whose scrollbars take room (Chrome with scroll bars always shown), the scrollbar that came with the taller page set the page measuring its cards without end.

## 0.4.0 -- 2026-10-21

SMART Sniffer is evolving, for the better. The Control Center now lives in the SMART Sniffer integration, with all your machines in one place. This App keeps watching this Home Assistant box's drives and shows them on its own page. Update the integration to 0.9 first, restart Home Assistant, then update this App. Your 0.3.x machines move to the integration by themselves; replicas need to be created again there.

### Changed
- This box, read only: the page shows this machine's drives, pools and filesystems, in the same design as the Control Center. A card below them opens the Control Center, or explains how to install or update the integration.
- The sidebar entry is now "SMART Sniffer App", and the page header reads "SMART SNIFFER | App · HAOS". No Refresh button: the page updates itself every 30 seconds.
- Drive cards match the Control Center: the at-a-glance row, the Details header, Device Statistics, and click-to-copy values.
- "Create sensors and alerts for HAOS" appears under the drives until the integration has this box.
- Home Assistant finds this box for you: with integration 0.9 or later, it is offered as "SMART Sniffer (HAOS)".
- Your 0.3.x machines move to the integration once, with their addresses, passwords, names, hidden cards, order and thresholds.
- Ships agent v0.9.0.

### Security
- The App's page answers only Home Assistant's ingress.
- The Bearer Token option shows as dots and is never passed on a command line.

### Removed
- Replicas (now in the integration, on every install type) and Agentic access (now in the Control Center). The Replica Agent Port option is removed at start.

### Upgrade notes
- Update the integration first. Next to integration 0.8.x, the App shows an update card and keeps your machines until you do.
- This box's thresholds from 0.3.x still apply on the App's page.

## 0.3.1 -- 2026-10-17

Adds **Agentic access**: everything the Control Center knows, as one JSON document for an AI assistant or a script. Disk space now shows for USB drives and the data disk, a Mac's filesystems sit on their drive's card, the page no longer opens blank while Home Assistant is busy, and the app ships with agent and rules from smart-sniffer v0.8.4.

### Added

- **Agentic access** -- a new footer link opens every agent's drives, verdicts, pools and filesystems as one JSON document, with a guide at the end for an AI reading it. **Copy**, **Download** and **Raw JSON** sit at the top. It needs your Home Assistant login, and tokens are never included.
- **USB drives and the data disk had no disk space bars** -- drives Home Assistant mounts under `/media`, and the data disk as `/data`, now get bars on their cards and Disk Usage entities in the integration. Restart the app after plugging in a USB drive. Thanks to @ellman121 (#11).

### Changed

- **A Mac's data volume showed as a card of its own** -- with agent 0.8.3, a macOS volume such as `/System/Volumes/Data` now sits on the card of the drive it lives on.
- **The card's add icon talked about adding to Home Assistant, from inside Home Assistant** -- it now asks to **Create** sensors and alerts for the machine, says what each drive gets, and for replicas creates test sensors to delete when you are done.
- **Without the integration, the page gave no way to get sensors** -- the grey icon now opens a short guide (install from HACS, restart, click again), and a one-line notice, dismissed once for every browser, points to the integration.
- **A pool or filesystem card did not say which machine it was on** -- every card's label now names its machine, as drive cards already did, and "ZFS pool" or "Filesystem" moves to the line under the title. A field agent's machine can be renamed from any of its cards.
- **Replicas looked like real drives at a glance** -- a replica card's REPLICA label is now a ghost, with a dotted frame and dimmer text. The rest of the card, health colours included, is unchanged.
- **Bundled agent v0.8.4** -- the app ships smart-sniffer agent v0.8.4, and the Control Center judges drives with the v0.8.4 rules and replicas.
- **New permission** -- `/media`, mounted read-only, to measure the USB drives' disk space.

### Fixed

- **The Control Center sometimes opened blank** -- this machine's drives waited for Home Assistant to answer several calls first, so a busy Home Assistant left the page empty for seconds. Now only the first read after the app starts waits for it, and until that reading arrives a card says "Reading this machine's drives."

## 0.3.0 -- 2026-10-12

The app's page is rebuilt as the **Control Center**: your drives' health on its own page, with or without the SMART Sniffer integration. It also shows the drives on your other machines that run the SMART Sniffer agent, and replicas, fake drives for testing Home Assistant, which replace the Test Lab. Built against smart-sniffer v0.8.2.

### Added

- **The Control Center** -- one grid of cards, one per drive and per ZFS pool, from this machine, your field agents and the replicas, and one for each filesystem that belongs to no drive or pool. A card shows where the drive is, its temperature, power-on hours and data written, its disk space and its health. The verdict (Attention, Maybe, Health OK, Health not known) comes from the integration's own rule modules, built into the image from the smart-sniffer release this version names, so the page and Home Assistant agree. The page header shows the app, agent and rules versions and whether this machine's agent is answering.
- **Health box and Details** -- under the health word, a box lists every row that caused it, with what was expected and the value. The chevron beside the word opens Details: every counter, Data Read, Device Statistics, the disk space in full and every attribute or NVMe log field as smartctl reports it.
- **Disk space bars** -- one per filesystem on the drive, or ZFS dataset on the pool, up to three per card. Below 80% a bar says how full it is and the size; from 80% it says how much is free, the number gets heavier at 90% and 95%, and at 95% the outline turns dashed.
- **Arranging and hiding cards** -- drag a card by the dots beside its title, or move it with the arrow keys. The eye hides a card for now; the eye beside Refresh shows the hidden cards again. The order and the hidden cards are kept in the app's options.
- **Renaming** -- click a card's title, or a field agent's machine label, and type a name in place. A drive or pool in Home Assistant takes the name as its device name there, and a rename made in Home Assistant shows on the card.
- **Removing** -- the trash removes a card for good with a second click on **Remove?**. A card in Home Assistant offers **Remove here**, or removing it and also disabling its device in Home Assistant while the agent still reports it, or deleting the device once the agent no longer reports it (the machine's whole entry when it is the last card). A replica in Home Assistant keeps the single **Remove?**, which also deletes its device and entities there. This machine's drives can only be hidden.
- **Thresholds per drive** -- the sliders icon in Details edits a drive's alert thresholds, the same ones the integration offers under Configure > Alert thresholds. **Accept current readings** and **Reset to defaults** fill the fields, and **Save** writes them. A drive in Home Assistant keeps its thresholds in the integration, so the card and Home Assistant judge it the same way; any other drive keeps them in the app's options until it is added to Home Assistant, when they move into the integration.
- **Field agents** -- your other machines that run the SMART Sniffer agent, with every drive and ZFS pool on its own card, judged by the same rules as your own. An agent with a token asks for it on its card; the app checks it with the agent before saving it and never shows it again. A machine that stops answering keeps its last readings, and its cards say when it was last seen. Field agents are stored in the app's options, so they are part of Home Assistant backups.
- **The + card** -- the last card adds field agents and replicas. Opening it scans the block your Home Assistant machine is on (prefilled from the Supervisor's network information) for agents on port 9099; nothing on the network is scanned until you open it, and blocks wider than /22 are refused. **Add by hand** checks one host name or address. **Replicas** lists the replica files to add, and **Upload a replica** puts a file in your replica folder.
- **Replicas** -- fake drives and pools with SMART data you control, served from JSON files by a replica agent inside the app. Six ship with the app; your own live in `/share/smart-sniffer/replicas`, where files copied in over Samba or SSH show up in the + card. Each replica card switches between scenarios with one click, and its Raw values can be edited in Details.
- **Save as replica** -- the download arrow at the foot of a card saves it as a JSON file. A real drive's copy goes into your replica folder with its serial number, WWN and NVMe identifiers replaced and the verdict it had recorded; a replica downloads with its current values. A replica file is at most 1 MB.
- **Add to Home Assistant from the page** -- with the integration installed, an exit icon on each card adds this machine, a field agent or the replicas to the integration (grey), or opens the device page in Home Assistant (blue). Details also gives the host and port for adding a machine by hand, with copy buttons.

### Changed

- **The integration is optional.** The page shows your drives' health without it; the integration adds entities, history, notifications and automations.
- **The replica agent always runs**, inside the app on port 9100, and does not advertise itself with mDNS, so Home Assistant never discovers it on its own. All replicas share one integration entry.
- **The web UI proxy behind ingress does more.** Besides serving the page and reading the agents, it runs the rule modules for the verdicts, reads field agents, and calls the Supervisor and Home Assistant for the options, the network information, adding, renaming and removing. The page's scripts and styles are named by their content, so a browser does not keep running the old page after an update.
- **The Mission Log panel is gone.** An **App log** link in the page footer opens the app's log in Home Assistant, which holds the agent's lines and the page's.
- **New permissions** -- Home Assistant API access (`homeassistant_api`), to add this app's agents, replicas and field agents to the integration and find, rename, disable or delete their devices; and `/share`, mounted read-write for the replica folder. The existing Supervisor access now also reads the network information behind the scan and writes the app's options.
- **New options** -- Field Agents, Card Order, Hidden Cards, Thresholds, Card Names and Machine Names, all set from the page. Writing them does not restart the app. Mock Agent Port is now Replica Agent Port.
- **The image takes the integration's rules, the replica agent and the shipped replicas** from one download of the smart-sniffer v0.8.2 release, and the build fails if fewer than six replica files load. This repository's own copy of the mock agent is gone.

### Removed

- **The Test Lab, its mock drives and the Enable Mock Test Drives (`mock_mode`) option.** Replicas take their place.

### Upgrading from 0.2.x

- The Supervisor leaves the old `mock_mode` setting in the app's options after the update. When the app starts, it removes every option key the app no longer has, in one write, and its log says so once.
- Mock drives you had in the Test Lab come back as replicas, and an integration entry you had for them now serves the replicas, so nothing needs to be added again.

## 0.2.12 — 2026-09-17

### Changed

- **Dropped 32-bit ARM (`armv7`) support** -- Home Assistant deprecated `armv7`, `armhf`, and `i386` in May 2025 and stopped building for them from release 2025.12. No 32-bit HAOS builds exist to run the App on, so this removes a declaration rather than a capability. Clears a Supervisor deprecation warning.
- **Build configuration moved into the Dockerfile** -- `build.yaml` and the unused `build.json` are gone. The Dockerfile now carries an explicit, pinned `FROM ghcr.io/home-assistant/base:3.24-2026.08.0` in place of the old `ARG BUILD_FROM` indirection, which Supervisor stopped populating by default in 2026.04.0. Same Alpine 3.24.1 as before -- no runtime change.

### Fixed

- **Restored the HTTP health watchdog** -- reverted the interim TCP check from v0.2.11 now that the bundled agent includes the `/api/health` auth exemption (agent v0.5.16+). HTTP confirms the application is actually healthy rather than merely that the port is open.

## 0.2.11 — 2026-06-10

### Fixed

- **Watchdog restart loop when bearer token is set** -- switched Supervisor watchdog from HTTP to TCP health check. The HTTP probe hit `/api/health` without a token, got 401, and Supervisor restart-looped the container (~every 4 min). TCP check confirms the port is accepting connections. Interim workaround -- will revert to HTTP when the App ships an agent binary with the auth bypass fix (v0.5.16+). See [#6](https://github.com/DAB-LABS/smart-sniffer-app/issues/6).

## 0.2.10 — 2026-03-27

### Fixed

- **Integration connection fix** — The Web UI and docs were showing `172.30.33.1` as the agent address. Now displays the correct container hostname (`0449a086-smart-sniffer-agent`) which resolves reliably in HAOS.

### Changed

- **Web UI** — Agent cards now show "Host" with the stable container hostname instead of a misleading IP. The hostname is selectable for easy copy-paste.
- **Startup log** — Now includes an `Integration host:` line showing the correct hostname and port for manual setup.
- **Docs** — All manual setup instructions updated to use the container hostname across DOCS.md, README.md, and the outer README.

## 0.2.9 — 2026-03-26

### Fixed

- **Bearer token + ingress/sidebar bug** — When a bearer token was configured, the sidebar Web UI (Agent Control Center) couldn't connect to the agent API. HA's ingress proxy doesn't pass the token header through, so the agent rejected all Web UI requests. The proxy now injects the bearer token on localhost requests automatically. External API access still requires the token as before.

### Added

- **Automatic drivedb.h updates** — The smartmontools drive database (`drivedb.h`) is now updated from upstream on every startup. This keeps SMART attribute definitions current for uncommon drives. The updated database is persisted to `/data` so it survives container rebuilds. Falls back silently to the bundled version if no internet is available.

### Changed

- **Protection Mode messaging softened** — Documentation no longer presents disabling Protection Mode as a mandatory step. The app works with Protection Mode ON for some hardware configurations. If your drive shows UNSUPPORTED or the logs show "DRIVE ACCESS BLOCKED," turning off Protection Mode is the recommended fix.

## 0.2.8 — 2026-03-25

### Added

- **Full hardware access** (`full_access: true`) — resolves the root cause of drive read failures across all hardware types (NVMe, SATA, M.2 SATA). Home Assistant's container security has four enforcement layers: Linux capabilities, AppArmor profiles, device cgroup rules, and the `/dev` mount mode. Previous releases addressed the first two (capabilities in v0.2.5, AppArmor in v0.2.7) but drives still returned "Operation not permitted" because the device cgroup blocked raw I/O to drive nodes. `full_access` tells the Supervisor to grant the device cgroup rule (`a *:* rwm`) that smartctl needs to open `/dev/sda`, `/dev/nvme0`, etc. This is the same approach used by Scrutiny and other drive monitoring tools on HAOS.

- **Startup drive access detection** — the app now probes the first detected drive during preflight and logs a clear "DRIVE ACCESS BLOCKED" warning if smartctl cannot open it, with step-by-step instructions to disable Protection Mode. When access is working, it logs "Drive access: OK (/dev/sda)".

- **Protection Mode documentation** — README and DOCS.md now include a dedicated section explaining why Protection Mode must be OFF for drive monitoring, what turning it off does, what the app does NOT do (no writes, no network access, no phoning home), and our commitment to open-source transparency. Includes a screenshot of the Protection Mode toggle.

- **Security & Permissions section** in README — documents every permission the app requests, why each is needed, and links to the source code for audit.

### Changed

- Updated troubleshooting guidance to lead with Protection Mode as the primary fix for "UNSUPPORTED" or missing SMART data.
- Terminology updated: HA "Add-ons" → "Apps" throughout docs to match current Home Assistant naming.

### Security Note

With `full_access: true`, the HA security score is approximately **4/8**. We ship a custom AppArmor profile that documents exactly what the container accesses. Protection Mode defaults to ON — users must disable it for drive monitoring. All code is open source at [github.com/DAB-LABS](https://github.com/DAB-LABS).

## 0.2.7 — 2026-03-25

### Added

- Custom AppArmor profile (`apparmor.txt`) for targeted drive access. The default HA AppArmor profile was blocking the system calls that `smartctl` needs to read SMART attributes from certain hardware (confirmed on Intel NUC with M.2 SATA SSD). This profile allows only the specific access smartctl requires while maintaining container security. Enables the **Protection Mode** toggle in the add-on UI. Security score improves from **6/8 to 7/8**.

## 0.2.6 — 2026-03-25

### Fixed

- Fixed startup log showing incorrect version (displayed "v0.2.3" instead of actual version). The `APP_VERSION` in `run.sh` was not updated in the v0.2.5 release. All version sources now align: config.yaml, run.sh, and CHANGELOG.

## 0.2.5 — 2026-03-25

### Fixed

- Added `SYS_ADMIN` container capability for NVMe drive support. Previously the app only requested `SYS_RAWIO`, which is sufficient for SATA drives but not for NVMe drives that require admin passthrough commands. Users running HAOS on NVMe (e.g., Raspberry Pi 5 with PCIe M.2 HAT) should now see their drives detected correctly. Security score remains 6/8 — no impact from this change.

## 0.2.4 — 2026-03-24

### Changed

- Bumped bundled agent binary to v0.4.28 via Docker cache bust

## 0.2.3 — 2026-03-24

### Changed

- Updated bundled agent to v0.4.28 with `--mdns-name` support
- Added `hassio_api: true` for Supervisor API access (required for hostname resolution)
- Docker cache bust to ensure fresh agent binary download on rebuild

## 0.2.2 — 2026-03-23

### Fixed

- mDNS collision fix: each HAOS instance now advertises a unique mDNS service name derived from the HA hostname via the Supervisor API, preventing discovery conflicts on multi-HA networks
- Guarded `--mdns-name` flag behind agent version check (requires agent v0.4.28+)

## 0.2.1 — 2026-03-22

### Added

- Agent IP address displayed on Web UI Operations Status card
- armv7 architecture support (Raspberry Pi 2/3/4 on 32-bit OS)

## 0.2.0 — 2026-03-22

### Fixed

- Set `init: false` to fix s6-overlay PID 1 conflict with Docker tini
- Removed custom AppArmor profile (using default)

### Changed

- Beta release with persistence and init fix

## 0.1.0 — 2026-03-21

### Added

- Initial release of the SMART Sniffer Agent app
- Go agent packaged from upstream `smartha-agent` releases
- Mock Test Lab with interactive simulated drives
- Unified Web UI ("Agent Control Center") served via HA ingress
  - Operations Status dashboard
  - Fleet Overview with color-coded attention states
  - Mock drive management with editable SMART attributes and real-time classification
  - Mission Log terminal viewer
- Multi-architecture support (amd64, aarch64)
- Configurable agent port, bearer token, scan interval
- Optional mock mode running alongside the real agent
- Python proxy server for ingress (static files + API/mock proxying)
- GitHub Actions CI for automated Docker builds
