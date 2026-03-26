# Changelog

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
