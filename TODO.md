# SMART Sniffer App — TODO

## Build / Packaging
- [x] **Migrate `build.yaml` to Dockerfile** — done in v0.2.12. Removed `build.yaml` and the unused `build.json`; Dockerfile now carries an explicit pinned `FROM ghcr.io/home-assistant/base:3.24-2026.08.0`. Note: Supervisor stopped supplying `BUILD_FROM` by default in 2026.04.0, so the explicit `FROM` was mandatory, not cosmetic — deleting `build.yaml` alone would have broken every build.
- [x] **Revert watchdog from TCP to HTTP** — done in v0.2.12. Bundled agent is well past v0.5.16, which exempts `/api/health` from auth.
- [x] **Drop armv7 from arch list** — done in v0.2.12. Removed from `config.yaml`, the CI build matrix, and the Dockerfile's arch case. See smart-sniffer#42.
- [ ] **Pin the bundled agent binary version** — the Dockerfile pulls `releases/latest/download`, so two installs of the same App version can carry different agent binaries depending on build date. This is what made the issue #6 timeline hard to reason about. Base image is now pinned; the agent binary is not.

## App Store Submission
- [x] **AppArmor profile** — Created `apparmor.txt` (v0.2.7). Previous v0.2.0 failure (`/init: Permission denied`) was caused by missing S6-Overlay rules. New profile includes full S6 init system access, targeted drive device access (`/dev/sd*`, `/dev/nvme*`, `/dev/sg*`), and network rules. Modeled on Scrutiny's working profile. Score: 7/8.
- [x] ~~Test on armv7 (Raspberry Pi 3/4 32-bit)~~ -- Dropping armv7 support instead. HA is deprecating 32-bit ARM.
- [ ] Publish container images to `ghcr.io/dab-labs/` (uncomment `image:` in config.yaml)
- [ ] Move from `stage: experimental` to `stage: stable`
- [ ] Add proper semantic versioning workflow
- [ ] Review HA add-on store submission requirements and guidelines

## Networking / Multi-Instance
- [x] **mDNS name collision** — resolved. `run.sh` now queries the Supervisor API for the HA hostname and passes `--mdns-name=smartha-<hostname>` to the Go agent (requires agent v0.4.28+). Each HA instance gets a unique mDNS name.
- [x] Agent uses HA instance hostname from Supervisor API in mDNS advertisement

## Integration-Side Changes (see also smart-sniffer repo)
- [ ] Mask mDNS hostname in discovery dialog with friendly name ("Found HAOS Drive")
- [ ] Mask 172.30.33.x IP with "Local" or "This system" in UI
- [ ] Auto-detect mock agent in "Add Device" config flow (check 172.30.33.1:9100)
- [ ] Guided mock setup flow ("Mock Test Lab detected — add it?")

## Web UI
- [ ] Show mock agent IP/port when mock mode is enabled
- [ ] Add drive detail expand/collapse
- [ ] Mobile-responsive layout improvements

## Done (v0.2.x)
- [x] CI/CD pipeline — GitHub Actions builds amd64, aarch64, armv7
- [x] Fix s6-overlay PID 1 error (`init: false` in config.yaml)
- [x] Fix Production install (`/init: Permission denied`) — removed AppArmor, version bump
- [x] Rewrite repo-level README.md as GitHub landing page
- [x] Add badges, installation instructions, architecture diagram
- [x] Add Web UI screenshot to README
- [x] Show agent IP address on Web UI (HAOS Drive Agent card)
- [x] Add CONTRIBUTING.md
- [x] Add GitHub issue templates (bug report, feature request)
- [x] Add Raspberry Pi (armv7) architecture support to pipeline
- [x] Add CHANGELOG.md
- [x] Add FUNDING.yml
- [x] Create social preview image
- [x] New header image (SMARTsniffer_app.png)
