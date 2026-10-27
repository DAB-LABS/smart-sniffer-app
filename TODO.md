# SMART Sniffer App -- TODO

## Build and packaging
- [x] **Migrate `build.yaml` to the Dockerfile** -- done in v0.2.12. The Dockerfile carries an explicit pinned `FROM ghcr.io/home-assistant/base`; the Supervisor stopped supplying `BUILD_FROM` by default in 2026.04.0.
- [x] **Watchdog back to HTTP** -- done in v0.2.12. The agent exempts `/api/health` from auth.
- [x] **Drop armv7** -- done in v0.2.12 (smart-sniffer#42).
- [x] **Pin the bundled agent** -- done in v0.4.0. The Dockerfile downloads the agent from the release named by `SS_TAG`, the same release the rule modules come from, and `test_dockerfile.py` fails if it is not pinned.

## App Store
- [x] **AppArmor profile** -- `apparmor.txt` since v0.2.7, with S6 init access and targeted drive device access (`/dev/sd*`, `/dev/nvme*`, `/dev/sg*`).
- [ ] Publish container images to `ghcr.io/dab-labs/` (uncomment `image:` in `config.yaml`), so installs stop building locally.
- [ ] Move from `stage: experimental` to `stage: stable`.
- [ ] Review the App Store submission requirements and guidelines.

## Discovery and multi-instance
- [x] **Unique mDNS name** -- `run.sh` passes `--mdns-name=smartha-<ha-hostname>`.
- [x] **Found for you** -- since v0.4.0 the App posts a Supervisor discovery and Home Assistant offers this box as "SMART Sniffer (HAOS)"; its devices carry the `HAOS` label instead of an internal address.

## The App's page
- [x] This box only, read only, in the Control Center's design -- v0.4.0. Field agents, replicas, the network scan and Agentic access moved to the integration.
- [ ] A dashboard card for the App's page. A Webpage card can show it today, but its ingress session expires.

## Agent (smart-sniffer repo)
- [x] Report the machine's `hostname` (agent v0.8.3) and `os_name` in `/api/health`.
- [ ] Report each ZFS pool's layout and full vdev tree, not only the problem vdevs.

## Done (v0.2.x)
- [x] CI/CD pipeline (GitHub Actions)
- [x] s6-overlay PID 1 fix (`init: false` in `config.yaml`)
- [x] README landing page, badges, installation steps, architecture diagram, screenshots
- [x] CONTRIBUTING.md, CHANGELOG.md, issue templates, FUNDING.yml
- [x] Social preview image and header image (`SMARTsniffer_app.png`)
