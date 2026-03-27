# SMART Sniffer App — TODO

## App Store Submission
- [x] **AppArmor profile** — Created `apparmor.txt` (v0.2.7). Previous v0.2.0 failure (`/init: Permission denied`) was caused by missing S6-Overlay rules. New profile includes full S6 init system access, targeted drive device access (`/dev/sd*`, `/dev/nvme*`, `/dev/sg*`), and network rules. Modeled on Scrutiny's working profile. Score: 7/8.
- [ ] Test on armv7 (Raspberry Pi 3/4 32-bit) — pipeline is ready, needs real hardware validation
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
