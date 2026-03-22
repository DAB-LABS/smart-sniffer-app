# Changelog

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
- **mDNS name collision** — multiple HA instances on the same network advertise the same mDNS service name. Workaround: add the integration manually via IP address (172.30.33.x:9099).
- **VM drives** — virtual disks (e.g. Proxmox VMs) may not support SMART commands. The agent detects them but cannot report SMART attributes.
- **AppArmor** — no custom profile; relies on the Supervisor default. May need a custom profile for official app store submission.

## [0.1.6] — 2026-03-22

Initial working version with mock agent, web UI, and ingress support.
