# Changelog

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
