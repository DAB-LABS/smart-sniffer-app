# SMART Sniffer App — TODO

## App Store Submission
- [ ] **AppArmor profile** — Create `apparmor.txt` for official store submission. Deferred: a custom profile caused `/init: Permission denied` on Production during beta testing. Revisit once the default profile is proven stable across architectures.
- [ ] Test on armv7 (Raspberry Pi 3/4 32-bit) — pipeline is ready, needs real hardware validation
- [ ] Publish container images to `ghcr.io/dab-labs/` (uncomment `image:` in config.yaml)
- [ ] Move from `stage: experimental` to `stage: stable`
- [ ] Add proper semantic versioning workflow
- [ ] Review HA add-on store submission requirements and guidelines

## Networking / Multi-Instance
- [ ] **mDNS name collision** — multiple HA instances on the same network advertise the same service name (`smartha-{slug}`). Each instance needs a unique mDNS name (e.g. include HA hostname or UUID) so zeroconf discovery works correctly on multi-HA networks. Requires changes in both the Go agent (`--mdns-name` flag) and this app's `run.sh`.
- [ ] Investigate whether the agent should use the HA instance's hostname (available via Supervisor API) in the mDNS advertisement

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
