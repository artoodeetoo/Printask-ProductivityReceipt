# Running Printask on Windows

Setup for Printask (Notion and Slack → Claude → network receipt printer) on Windows 10/11 with Docker Desktop. For the Notion setup and the full variable list, see [README.md](../README.md).

> **Status:** **not tested on Windows.** The Docker image is a Linux image, which is what Docker Desktop on Windows runs, and it is the same image that runs the app on a Synology NAS against live Notion and a Rongta ACE-H1 printer, so it should behave the same. The Windows-specific steps below (WSL 2, PowerShell, auto-start, the no-Docker route) are written from general knowledge and have not been run on Windows.

A Windows PC is fine for trying this out or running it while the PC is on and you are signed in. For an always-on setup, a server or NAS is better (see [SYNOLOGY_SETUP.md](SYNOLOGY_SETUP.md)).

## Requirements

- Windows 10/11 (64-bit) with [Docker Desktop](https://www.docker.com/products/docker-desktop/) using the **WSL 2** backend (the default). Use **Linux containers**, not Windows containers.
- [Git for Windows](https://git-scm.com/download/win) (or download the repo as a ZIP).
- The printer's IP address, reachable from the PC on port 9100 (same network or VPN). Give the printer a fixed IP or DHCP reservation.

## Option A: Docker (recommended)

In PowerShell:

```powershell
git clone https://github.com/artoodeetoo/Printask.git
cd Printask
Copy-Item .env.example .env
notepad .env
# Fill in PRINTER_HOST, plus a source: NOTION_API_KEY + NOTION_DATABASE_ID and/or
# SLACK_BOT_TOKEN + SLACK_CHANNEL_IDS. ANTHROPIC_API_KEY is optional for Notion, required for Slack.

# One-shot test: print the current To Do tasks once and exit
docker compose run --rm -e POLL_INTERVAL_SECONDS=0 app

# Run continuously in the background
docker compose up -d --build
docker compose logs -f app
```

Use `run --rm` for the one-shot test, not `up`: with `restart: unless-stopped` a container that exits after one run would be restarted repeatedly.

**Heads-up:** the first run prints *every* task whose Status is `To Do` / `Not started` that hasn't printed yet. If your list is long, set most of them to another status first. Check the printer with **Print test card** in the web UI (below) before you run it for real.

Notes:
- Docker Desktop must be running. Enable **Settings → General → Start Docker Desktop when you sign in** so the container comes back after a reboot.
- Save `.env` as plain text named exactly `.env` (Notepad can add `.txt`; check in File Explorer with file extensions shown). Values need no quotes.
- Task history lives in the `printer-data` volume, so restarts do not reprint tasks. `docker compose down -v` deletes it and reprints everything.
- Windows Firewall normally allows outbound connections to the printer. If the connection fails, check that the PC and printer are on the same network and that no VPN is blocking local traffic.

## Option B: Without Docker

Useful for development.

```powershell
py -3.13 -m venv venv
.\venv\Scripts\Activate.ps1
pip install -r requirements.txt
Copy-Item .env.example .env     # then edit it
$env:HISTORY_PATH = ".\history.db"     # the default /data paths do not exist on Windows
$env:SETTINGS_PATH = ".\settings.json"  # where the web UI saves settings (needed to save them)
python -m simple_printer
```
If activation is blocked, run `Set-ExecutionPolicy -Scope CurrentUser RemoteSigned` once. Set `POLL_INTERVAL_SECONDS=0` to run once. Run the tests with `pytest tests/`. Card rendering uses Windows fonts (Arial) here, so output may differ slightly from the Docker image.

## Optional: Slack and the web UI

- **Slack** as a task source: see "Slack setup" in [../README.md](../README.md), then add `SLACK_BOT_TOKEN` and `SLACK_CHANNEL_IDS` (and `ANTHROPIC_API_KEY`) to `.env`.
- **Web UI** with a Dashboard, a Tasks tab (including clearing the history) and Settings, plus Poll now, Print test card and Preview Slack buttons: add `UI_PORT=8081` and a long `UI_PASSWORD` to `.env`, run `docker compose up -d`, and open <http://localhost:8081>. Settings you save there are stored in the Docker volume.
- **More features**, such as the category printed on the card, writing Claude's priority back to Notion, and how the priority is decided, are explained in [../README.md](../README.md).

After changing `.env`, run `docker compose up -d` again to apply it.

## Troubleshooting

| Symptom | Likely cause |
|---|---|
| `error during connect` / cannot reach the Docker engine | Docker Desktop is not running, or WSL 2 isn't enabled |
| Exit with `Missing required environment variables` or `No task source configured` | `.env` missing (or saved as `.env.txt`), `PRINTER_HOST` empty, or neither Notion (key + database ID) nor Slack (token + channel IDs) is fully set |
| `port is already allocated` / `ports are not available` | Something else on the PC uses port 8081, which compose always publishes. Set `UI_PORT=8091` (or another free port) in `.env` and run `docker compose up -d` again. Windows can also reserve port ranges; a different port avoids that |
| Web UI doesn't load | Both `UI_PORT` and `UI_PASSWORD` must be set in `.env`; use `http://`, not `https://`; run `docker compose up -d` after editing |
| Slack errors (`not_in_channel`, `missing_scope`, `invalid_auth`) or all Slack cards are MEDIUM | See "Slack setup" and "How priority is decided" in [../README.md](../README.md) |
| Web UI says it could not write the settings file | Only when running without Docker: set `$env:SETTINGS_PATH = ".\settings.json"` |
| I want a task to print again | Use **Clear history** on the Tasks tab, or see "Clearing the history" in [../README.md](../README.md) |
| `Notion API error: 401` / `404` | Wrong token or database ID, or the database isn't shared with the integration |
| `Device not found` / socket errors | PC can't reach `PRINTER_HOST:9100` (wrong IP, printer off, different network/VPN) |
