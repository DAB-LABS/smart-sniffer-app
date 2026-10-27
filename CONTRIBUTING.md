# Contributing to SMART Sniffer App

Thanks for your interest in contributing. This App watches the drives of the machine Home Assistant runs on, and every improvement helps keep people's systems safer.

## Getting started

1. **Fork** this repo and clone your fork.
2. Create a feature branch: `git checkout -b my-feature`.
3. Make your changes, and run the tests (below).
4. Push and open a pull request against `main`.

## How the App is built

- **The agent** (`smartha-agent`) reads SMART data from the host's drives with `smartctl`, serves it over HTTP on port 9099, and advertises itself with mDNS. It is the same Go agent the [integration repo](https://github.com/DAB-LABS/smart-sniffer) releases. The Dockerfile downloads it from the release named by `SS_TAG`, along with the integration's rule modules, so an App version always ships the agent and rules of the version it names.
- **The page** (`smart_sniffer_agent/web/`) is the App's read-only page, served through Home Assistant's ingress by `web/proxy.py`. It shows this box's drives, pools and filesystems in the same design as the integration's Control Center, judged by the integration's own rules. `proxy.py` also posts the Supervisor discovery that lets the integration find this box, and hands the machines App 0.3.x kept over to the integration.
- **Startup** (`run.sh`, `mounts.py`) reads the App's options, writes the agent's config (disk space mounts, and the token, root only), and starts the agent and the page.

Everything else, other machines, replicas, entities and the Control Center, lives in the [SMART Sniffer integration](https://github.com/DAB-LABS/smart-sniffer).

## Development setup

### The page, without Home Assistant

`web/devserver.py` serves the page and fakes every route it calls, so you can work on it without the App, the agent or Home Assistant. It needs Python 3.9 or later and the real Alpine and Tailwind files (the repo keeps placeholders):

```bash
./setup-dev.sh                       # downloads alpine.min.js and tailwind.min.js into web/
cd smart_sniffer_agent/web
python3 devserver.py                 # http://127.0.0.1:8099/
```

Switches in the environment or the page's query string pick what it shows, for example `?DRIVES=mixed&POOL=degraded` or `?INTEGRATION=missing`. The top of `devserver.py` lists them all. Point `SS_RULES_DIR` at an integration checkout's `custom_components` folder for the real verdicts. Do not commit the downloaded vendor files.

### The App, on a Home Assistant box

1. Install the **Samba share** App and mount the `addons` share on your machine.
2. Run `./setup-dev.sh`, then copy the `smart_sniffer_agent/` folder into the `addons` share.
3. In Home Assistant, go to **Settings > Apps > App Store**, open the three-dot menu and choose **Check for updates**.
4. Install the local App and check its log.

Or push your fork and add it as a repository: **Settings > Apps > App Store**, three-dot menu, **Repositories**.

## Tests

```bash
pip install pytest playwright
cd smart_sniffer_agent
python3 -m pytest test_mounts.py test_dockerfile.py
python3 -m pytest web
```

The `web/` tests drive the page in Chromium through Playwright and need the real vendor files: run `./setup-dev.sh` first, or set `VENDOR_DIR` to a folder that holds them. Run them one at a time (no `-n`); some share state.

`test_dockerfile.py` fails if the agent is not downloaded from the `SS_TAG` release.

## What can you work on?

Check [TODO.md](TODO.md) for tracked items, or look at the [open issues](https://github.com/DAB-LABS/smart-sniffer-app/issues). Good first contributions include:

- Testing on Raspberry Pi (aarch64) and reporting results
- Improvements to the page
- Documentation and troubleshooting guides
- Bug reports with logs and system details

## Code style

- **Shell scripts**: bash, `shellcheck` clean, arguments in arrays and quoted.
- **Python**: standard library only in the App.
- **The page**: plain HTML, CSS and JavaScript with Alpine.js and Tailwind, no build step. It follows the integration's Control Center panel; keep the two in step.
- **Commits**: short, descriptive messages. Reference issue numbers when there is one.

## Reporting issues

Use the [bug report template](https://github.com/DAB-LABS/smart-sniffer-app/issues/new?template=bug_report.yml) and include:

- Home Assistant version and installation type (HAOS, Supervised)
- Hardware (x86, Raspberry Pi 4 or 5, and so on)
- App version (on the App's page header, or **Settings > Apps > SMART Sniffer App**)
- The App's log (**Settings > Apps > SMART Sniffer App > Log**)

## Pull requests

- Keep pull requests focused: one feature or fix each.
- Test on at least one architecture before submitting.
- Update `CHANGELOG.md` (both the root file and `smart_sniffer_agent/CHANGELOG.md`) if your change is user-facing.
- The CI pipeline builds the image for amd64 and aarch64.

## Updating llms.txt

`llms.txt` in the repo root is a machine-readable project summary used by LLMs and AI-powered search to describe and recommend this project. Keep it current.

**Update `llms.txt` when your pull request:**

- changes `README.md` in a way that affects features, capabilities, supported architectures, configuration options or the documentation structure,
- adds a feature or fixes a known issue in a way that changes how the App behaves,
- changes architecture support.

**Format rules:**

- Follow the llmstxt.org spec: H1, blockquote summary, body, H2 link sections.
- Keep the file under about 200 lines.
- No em dashes: use double hyphens (--), parentheses or separate sentences.
- Every claim must be verifiable against the README or the CHANGELOG.

## License

By contributing, you agree that your contributions will be licensed under the [MIT License](LICENSE).
