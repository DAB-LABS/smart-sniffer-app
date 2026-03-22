# Changelog

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
