![SMART Sniffer App for Home Assistant OS](https://raw.githubusercontent.com/DAB-LABS/smart-sniffer-app/main/smart_sniffer_agent/SMARTsniffer_app.png)

**The drives inside your Home Assistant box, watched.** *Sniff early. Sniff often.*

The SMART Sniffer App watches the drives of the machine Home Assistant runs on: the boot SSD or NVMe, the data disk, and any USB drive Home Assistant mounts. It reads their S.M.A.R.T. data, judges every drive with the same rules as the SMART Sniffer integration, and shows them on its own page.

![The SMART Sniffer App page](https://raw.githubusercontent.com/DAB-LABS/smart-sniffer-app/main/images/app-page.png)

**Better together.** Pair it with the [SMART Sniffer integration](https://github.com/DAB-LABS/smart-sniffer) (from HACS, v0.9 or later) for sensors, history, notifications and automations, and a Control Center with every machine you run. The App tells Home Assistant where its agent is, so this box is added with one click.

- Home Assistant OS or Supervised. In a VM the App sees only the virtual disk; install the agent on the host instead.
- Some hardware needs Protection Mode off before drives can be read. The Documentation tab explains.

See the **Documentation** tab for the page, configuration, upgrading from 0.3.x and troubleshooting.
