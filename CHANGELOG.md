# Changelog

## [Unreleased]

### Added

- **Disk space for USB drives and the data disk** -- USB drives Home Assistant mounts under `/media` get disk space bars on their cards and Disk Usage entities in the integration. So does the data disk, shown as `/data`, whether it is still on the boot drive or on a drive picked with **Move data disk**. The drives are found when the app starts; restart the app after plugging in a USB drive.

### Changed

- **New permission** -- `/media`, mounted read-only, to measure the USB drives' disk space.

## [0.3.0] -- 2026-10-12

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

## [0.2.12] — 2026-09-17

### Changed

- **Dropped 32-bit ARM (`armv7`) support** -- Home Assistant [deprecated `armv7`, `armhf`, and `i386`](https://www.home-assistant.io/blog/2025/05/22/deprecating-core-and-supervised-installation-methods-and-32-bit-systems/) in May 2025 and stopped building for them from release 2025.12. There are no 32-bit HAOS builds to run the App on, so this removes a declaration rather than a capability. Clears a Supervisor deprecation warning. See [smart-sniffer#42](https://github.com/DAB-LABS/smart-sniffer/issues/42).
- **Migrated build configuration into the Dockerfile** -- removed `build.yaml` and the unused `build.json`, and replaced the `ARG BUILD_FROM` / `FROM ${BUILD_FROM}` pair with an explicit, pinned `FROM ghcr.io/home-assistant/base:3.24-2026.08.0`. Supervisor stopped supplying `BUILD_FROM` by default in 2026.04.0, so the explicit base image is now required. Base images ship as multi-platform manifests, so one `FROM` covers both supported architectures. Clears the second Supervisor deprecation warning.
- **Pinned the base image** -- previously tracked `latest`. The pinned tag resolves to the same Alpine 3.24.1 image in use today, so nothing changes at runtime; it just stops the base from drifting between builds.

### Fixed

- **Restored the HTTP health watchdog** -- reverted from the interim TCP check introduced in v0.2.11. The agent-side fix that exempts `/api/health` from bearer-token auth shipped in agent v0.5.16, and the bundled binary is now well past that. HTTP is the stronger signal: it confirms the agent process is alive, the HTTP server is responding, and the application is healthy, not merely that something holds the port. See [#6](https://github.com/DAB-LABS/smart-sniffer-app/issues/6).

## [0.2.11] — 2026-06-10

### Fixed

- **Watchdog restart loop when bearer token is set** -- switched Supervisor watchdog from HTTP to TCP health check. The HTTP probe hit `/api/health` without a token, got 401, and Supervisor restart-looped the container. TCP check confirms the port is accepting connections, which is sufficient to keep the container alive. This is an interim workaround until the App ships an agent binary with the auth bypass fix (v0.5.16+), at which point the watchdog will revert to HTTP. See [#6](https://github.com/DAB-LABS/smart-sniffer-app/issues/6).

## [0.2.2] — 2026-03-24

### Added
- **Raspberry Pi (armv7) support** — CI pipeline now builds for amd64, aarch64, and armv7. Untested on real hardware — testers welcome.
- **CONTRIBUTING.md** — development setup, architecture overview, and PR guidelines.
- **GitHub issue templates** — structured bug report and feature request forms.
- **Web UI screenshot** in README — Agent Control Center showing both agent cards with IP addresses.
- **Raspberry Pi testers callout** in README with banner image.

### Fixed
- **mDNS name collision on multi-HA networks** — the app now queries the Supervisor API for the HA hostname and passes `--mdns-name=smartha-<hostname>` to the agent (requires agent v0.4.28+). Each HA instance gets a unique mDNS service name, so both appear in discovery.
- **CI lint step** — was referencing `build.yaml` instead of `build.json`.

### Note on Upgrade
After updating to v0.2.2, the integration may **re-discover** the HAOS drive agent as a new device. This is expected — the agent is now advertising under a new mDNS name based on your HA hostname instead of the generic container hostname. Your existing drive connection still works. You can safely dismiss the new discovery notification.

## [0.2.0] — 2026-03-22

First beta release of the SMART Sniffer App for Home Assistant.

### Added
- **Mock drive persistence** — mock drives survive app restarts with the same entity IDs. No more re-adding drives or losing HA entities after a stop/start cycle. Drive state is saved to `/data/mock-drives.json`.
- **`build.json`** — specifies HA base images for each architecture so the Supervisor builds correctly from the repo.
- **CI/CD pipeline** — GitHub Actions workflow builds and validates for both amd64 and aarch64. Publishes container images to GHCR on version tags.
- **Repo docs** — LICENSE (MIT), .gitignore, TODO.md.

### Changed
- **Renamed** from "SMART Sniffer Agent" to "SMART Sniffer App" with updated description.
- **Logo header** — README.md and DOCS.md use the SMARTsniffer.png logo instead of a plain text title.
- **Architecture diagram** — embedded in DOCS.md and README.md via GitHub raw URLs.
- **`init: false`** — required for s6-overlay v3 compatibility. Prevents Docker's tini from stealing PID 1.

### Removed
- **AppArmor profile** — removed `apparmor.txt` temporarily to unblock installs. The default Supervisor profile is used instead. Custom AppArmor will be revisited for official app store submission.
- **Remove buttons** — removed from mock drive cards in the web UI.

### Fixed
- s6-overlay PID 1 error (`s6-overlay-suexec: fatal: can only run as pid 1`) resolved by setting `init: false`.
- Production install failure (`/init: Permission denied`) resolved by removing custom AppArmor profile.

### Known Issues
- ~~**mDNS name collision**~~ — fixed in v0.2.2.
- **VM drives** — virtual disks (e.g. Proxmox VMs) may not support SMART commands. The agent detects them but cannot report SMART attributes.
- **AppArmor** — no custom profile; relies on the Supervisor default. May need a custom profile for official app store submission.

## [0.1.6] — 2026-03-22

Initial working version with mock agent, web UI, and ingress support.
