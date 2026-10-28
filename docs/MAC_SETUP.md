# Running Printask on a Mac

Setup for Printask (Notion and Slack → Claude → network receipt printer) on macOS with Docker. For the Notion setup and the full variable list, see [README.md](../README.md).

> **Status:** the Docker image builds natively on Apple silicon (arm64) and the test suite passes inside it. The same image runs the app on a Synology NAS against live Notion and a Rongta ACE-H1 printer. The Mac steps below (Docker Desktop, auto-start, the no-Docker route) have not been walked through on a Mac end to end.

A Mac is fine for trying this out or running it while the Mac is awake and logged in. For an always-on setup, a server or NAS is better (see [SYNOLOGY_SETUP.md](SYNOLOGY_SETUP.md)).

## Requirements

- macOS with [Docker Desktop](https://www.docker.com/products/docker-desktop/) (or another engine such as OrbStack or Colima that provides `docker compose`). Intel and Apple-silicon Macs both work; the image builds natively for either.
- The printer's IP address, reachable from the Mac on port 9100 (same network or VPN). Give the printer a fixed IP or DHCP reservation.

## Option A: Docker (recommended)

```bash
git clone https://github.com/artoodeetoo/Printask.git
cd Printask
cp .env.example .env
# Edit .env: PRINTER_HOST, plus a source: NOTION_API_KEY + NOTION_DATABASE_ID and/or
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
- Docker Desktop must be running for the container to run. To keep it running across restarts, enable **Settings → General → Start Docker Desktop when you sign in**. The container then restarts with it (`restart: unless-stopped`).
- Task history lives in the `printer-data` volume, so restarts do not reprint tasks. `docker compose down -v` deletes it and reprints everything.
- Stop with `docker compose down`. Update with `git pull && docker compose up -d --build`.

## Option B: Without Docker

Useful for development.

```bash
python3 -m venv venv && source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env     # then edit it
export HISTORY_PATH=./history.db     # the default /data paths are not writable on a Mac
export SETTINGS_PATH=./settings.json  # where the web UI saves settings (needed to save them)
python -m simple_printer
```
Set `POLL_INTERVAL_SECONDS=0` to run once. Run the tests with `pytest tests/`. Card rendering uses macOS system fonts here, so output may differ slightly from the Docker image, which uses DejaVu.

## Optional: Slack and the web UI

- **Slack** as a task source: see "Slack setup" in [../README.md](../README.md), then add `SLACK_BOT_TOKEN` and `SLACK_CHANNEL_IDS` (and `ANTHROPIC_API_KEY`) to `.env`.
- **Web UI** with a Dashboard, a Tasks tab (including clearing the history) and Settings, plus Poll now, Print test card and Preview Slack buttons: add `UI_PORT=8081` and a long `UI_PASSWORD` to `.env`, run `docker compose up -d`, and open <http://localhost:8081>. Settings you save there are stored in the Docker volume.
- **More features**, such as the category printed on the card, writing Claude's priority back to Notion, and how the priority is decided, are explained in [../README.md](../README.md).

After changing `.env`, run `docker compose up -d` again to apply it.

## Troubleshooting

| Symptom | Likely cause |
|---|---|
| `Cannot connect to the Docker daemon` | Docker Desktop is not running |
| Exit with `Missing required environment variables` or `No task source configured` | `.env` missing, `PRINTER_HOST` empty, or neither Notion (key + database ID) nor Slack (token + channel IDs) is fully set |
| `port is already allocated` / `address already in use` | Something else on the Mac uses port 8081, which compose always publishes. Set `UI_PORT=8091` (or another free port) in `.env` and run `docker compose up -d` again |
| Web UI doesn't load | Both `UI_PORT` and `UI_PASSWORD` must be set in `.env`; use `http://`, not `https://`; run `docker compose up -d` after editing |
| Slack errors (`not_in_channel`, `missing_scope`, `invalid_auth`) or all Slack cards are MEDIUM | See "Slack setup" and "How priority is decided" in [../README.md](../README.md) |
| Web UI says it could not write the settings file | Only when running without Docker: set `SETTINGS_PATH=./settings.json` |
| I want a task to print again | Use **Clear history** on the Tasks tab, or see "Clearing the history" in [../README.md](../README.md) |
| `Notion API error: 401` / `404` | Wrong token or database ID, or the database isn't shared with the integration |
| `Device not found` / socket errors | Mac can't reach `PRINTER_HOST:9100` (wrong IP, printer off, different network/VPN) |
| `Permission denied` on `/data/history.db` | Only happens with a bind mount (the folder must be writable by uid 1000); the default named volume works |
