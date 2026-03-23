# SMART Sniffer App — TODO

## GitHub README Redesign
- [ ] Rewrite repo-level `README.md` to serve as a proper GitHub landing page
- [ ] Explain why this is a separate repo from the integration (`smart-sniffer`)
- [ ] Clarify the relationship: App = local HAOS drive monitoring + test lab, Integration = HA entities + network agents
- [ ] Add badges (version, license, HA compatibility)
- [ ] Include installation instructions for both add-on store and local dev
- [ ] Link to integration repo, HACS, and documentation
- [ ] Consider adding screenshots of the app info page and web UI

## App Store Submission
- [ ] Create `apparmor.txt` — custom AppArmor profile (required for official store)
- [ ] Test on aarch64 (Raspberry Pi) — currently only tested on amd64
- [x] Set up CI/CD pipeline (GitHub Actions) for automated Docker builds
- [ ] Publish container images to `ghcr.io/dab-labs/` (uncomment `image:` in config.yaml)
- [ ] Move from `stage: experimental` to `stage: stable`
- [ ] Add proper semantic versioning workflow
- [ ] Review HA add-on store submission requirements and guidelines

## Networking / Multi-Instance
- [ ] **mDNS name collision** — multiple HA instances on the same network advertise the same service name (`smartha-{slug}`). Each instance needs a unique mDNS name (e.g. include HA hostname or UUID) so zeroconf discovery works correctly on multi-HA networks
- [ ] Investigate whether the agent should use the HA instance's hostname (available via Supervisor API) in the mDNS advertisement

## Integration-Side Changes (see also smart-sniffer/TODO-integration.md)
- [ ] Mask mDNS hostname in discovery dialog with friendly name ("Found HAOS Drive")
- [ ] Mask 172.30.33.x IP with "Local" or "This system" in UI
- [ ] Auto-detect mock agent in "Add Device" config flow (check 172.30.33.1:9100)
- [ ] Guided mock setup flow ("Mock Test Lab detected — add it?")

## Web UI Improvements
- [ ] **Show agent IP address on the web UI** — display the agent's network address (e.g. `172.30.33.1:9099`) on the Agent Control Center page so users can easily find the IP for manual integration setup
- [ ] Show mock agent IP/port when mock mode is enabled

## Polish
- [ ] Add CONTRIBUTING.md
- [ ] Add GitHub issue templates
- [ ] Add screenshot/demo GIF to repo README
- [ ] Update CHANGELOG.md with recent changes
