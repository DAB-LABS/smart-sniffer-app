# Changelog

## [0.3.0] (unreleased)

### Added

- **Field agents**: the Control Center now shows the drives on your other machines that run the SMART Sniffer agent, one card per machine, with every drive and ZFS pool judged by the same rules as your own. Agents that use a token ask for it on their card; the app checks it with the agent before saving it and never shows it again.
- **The + card and the network scan**: one card adds field agents and replicas. Opening it scans the block your Home Assistant machine is on (prefilled from the Supervisor's network information) for agents on port 9099, and nothing on the network is scanned until you open it. Blocks wider than /22 are refused. **Add by hand** checks a single host name or address.
- **Replicas as files**: the Test Lab's simulated drives are now replicas, fake drives and pools served from JSON files. Six ship with the app; your own live in `/share/smart-sniffer/replicas`, where files dropped in over Samba appear in the + card. Each replica has scenarios to apply and, in its Field report, editable Raw values.
- **Save as replica**: any real drive with SMART data can be saved as a replica file, with its serial number, WWN and NVMe identifiers replaced and the verdict it had at the time recorded.
- **Download and Upload**: a replica can be downloaded with its current values and uploaded again (1 MB at most).
- **Add to Home Assistant from the page**: with the integration installed, an exit glyph after each card's title adds this machine, a field agent or the replicas to the integration (grey), or opens the entry or device page in Home Assistant (blue).
- **Hiding cards**: the eye on a field agent card hides it; the eye in the section header shows the hidden cards again.

### Changed

- **The Control Center page is rebuilt** in three sections, Case file, Field agents and Replicas, and works on its own: the SMART Sniffer integration is no longer needed to see your drives' health. The verdict on every card (Attention, Maybe, Health OK, Health not known) comes from the integration's own rule modules, built into the image from the smart-sniffer release this version names, so the page and Home Assistant agree.
- **The replica agent always runs**, inside the app on port 9100, and does not advertise itself with mDNS, so Home Assistant never discovers it on its own. All replicas share one integration entry.
- **Field agents are stored in the app's options** (the new **Field Agents** option), so they are part of Home Assistant backups. Adding, hiding and saving a password write the options once and do not restart the app.
- **New permissions**: Home Assistant API access (`homeassistant_api`), to add this app's agents, replicas and field agents to the integration and find their device pages; and `/share`, mounted read-write for the replica folder. The app's existing Supervisor access now also reads the network information behind the scan and stores the field agents.
- **The image takes the integration's rules, the replica agent and the shipped replicas** from one download of the smart-sniffer release, and the build fails if fewer than six replica files load. The stale `mock-agent.py` copy in this repository is gone.

### Removed

- **The Test Lab switch and the `mock_mode` option** (Enable Mock Test Drives). The Supervisor drops the option when the app updates. Drives you had in the Test Lab come back as replicas, and an integration entry you had for the Test Lab now serves the replicas.
- The Control Center's Connect and Disconnect buttons. Adding is the exit glyph; removing an entry happens in Home Assistant.

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
