# Contributing to SMART Sniffer App

Thanks for your interest in contributing! This project monitors the health of Home Assistant system drives, and every improvement helps keep people's systems safer.

## Getting Started

1. **Fork** this repo and clone your fork
2. Create a feature branch: `git checkout -b my-feature`
3. Make your changes
4. Push and open a Pull Request against `main`

## Development Setup

The app runs as an HA add-on, so you'll need a Home Assistant instance for testing.

### Local Development (SAMBA)

1. Enable the **Samba** add-on in HA
2. Mount the `addons` share on your machine
3. Copy the `smart_sniffer_agent/` folder into the `addons` share
4. In HA, go to **Settings → Add-ons → Add-on Store**, click **⋮ → Check for updates**
5. Install the local add-on and check the logs

### Repository Install (HACS)

1. Fork the repo and push your changes
2. Add your fork as a custom repository in **Settings → Add-ons → Repositories**
3. Install and test from there

### Mock Test Lab

Enable **Mock Test Drives** in the app config to test without touching real hardware. Mock drives behave identically to real drives from the integration's perspective — you can adjust SMART attributes in the web UI and watch the integration respond.

## What Can You Work On?

Check the [TODO.md](TODO.md) for tracked items, or look at [open issues](https://github.com/DAB-LABS/smart-sniffer-app/issues). Good first contributions include:

- Testing on Raspberry Pi (armv7, aarch64) and reporting results
- Web UI improvements
- Documentation and troubleshooting guides
- Bug reports with logs and system info

## Architecture Overview

The app has three main components:

- **Go Agent** (`smartha-agent`) — reads SMART data from host drives via `smartctl`, serves it over HTTP, and advertises via mDNS. The binary is downloaded at build time from the [integration repo releases](https://github.com/DAB-LABS/smart-sniffer/releases).
- **Mock Agent** (`mock-agent.py`) — Python script that simulates drives with configurable SMART attributes for testing.
- **Web UI** (`web/index.html`) — Alpine.js + Tailwind dashboard served via HA ingress. Shows agent status, drive data, and mock controls.

## Code Style

- **Shell scripts** — POSIX-compatible where possible, `bashate` clean
- **Python** — Standard library preferred, minimal dependencies
- **HTML/JS** — Single-file approach (Alpine.js + Tailwind), no build step
- **Commits** — Short, descriptive messages. Reference issue numbers when applicable.

## Reporting Issues

Use the [bug report template](https://github.com/DAB-LABS/smart-sniffer-app/issues/new?template=bug_report.yml) and include:

- HA version and installation type (HAOS, Supervised, etc.)
- Hardware (x86, Pi 4, Pi 5, etc.)
- App version (from the add-on info page)
- App logs (Settings → Add-ons → SMART Sniffer App → Log)

## Pull Requests

- Keep PRs focused — one feature or fix per PR
- Test on at least one architecture before submitting
- Update `CHANGELOG.md` if your change is user-facing
- The CI pipeline will build and lint automatically

## License

By contributing, you agree that your contributions will be licensed under the [MIT License](LICENSE).
