# Running Printask on a Synology NAS

> **Status:** set up on a DS1618+ (x86_64) running the older **Docker package, engine 20.10.3** (DSM 7.0/7.1 era), deployed through **Portainer** (Route A). In that setup the container started, picked up a new Notion task, printed its card on the network printer (checked by eye) and recorded it as printed. (That run predates the SQLite history; the history and duplicate check are covered by tests only, not yet run on the NAS.) Route B (DSM 7.2+ with Container Manager) is written from general knowledge and has **not** been tested on a Synology. Nothing here was verified against ARM models.

> **Hardware I used:** a **[Synology DS1618+](https://link.amazon/B0ijYOE9h)**. *Amazon affiliate link: if you buy through it, I earn a small commission at no extra cost to you. As an Amazon Associate I earn from qualifying purchases.* The steps below were run on that model only.

Which route do you have? In a terminal on the NAS, run `sudo docker version`.

- **Engine 20.x with the "Docker" package, and no `docker compose` command** → **Route A** (build on a PC, load the image, deploy a Portainer stack).
- **DSM 7.2+ with Container Manager** (Package Center) → **Route B** (Compose Project). Updating DSM to 7.2 is also the cleanest fix for an old engine: it gives you a much newer Docker.

Either way you need the printer's own IP address, reachable from the NAS on port 9100. Give the printer a fixed IP or DHCP reservation. Test from the NAS:
```bash
sudo docker run --rm busybox nc -zv <printer-ip> 9100     # expect: open
```
Notion setup and the full variable list are in [README.md](../README.md).

---

## Route A: older Docker package + Portainer

The NAS has no `git`, no `docker compose`, and an old engine, so you build the image on a PC and load it.

### 1. Build the image on your Mac or PC

Docker Desktop must be running. `--platform linux/amd64` matters: an Apple-silicon Mac otherwise builds an arm64 image that won't run on the DS1618+.
```bash
git clone https://github.com/artoodeetoo/Printask.git     # private repo: sign in, or use a token
cd Printask
docker build --platform linux/amd64 -t printask:latest .
docker save -o printask.tar printask:latest
```

### 2. Load the image on the NAS

Either upload `printask.tar` in Portainer (**Images → Import**), or copy it to the NAS (File Station, or `scp`) and run:
```bash
cd /volume1/docker
sudo docker load -i printask.tar          # expect: Loaded image: printask:latest
sudo docker images | grep printask
```
If you skip this step, the stack fails with `pull access denied for printask`. Docker is trying to download the image because it isn't on the NAS.

### 3. Create the stack in Portainer

**Stacks → Add stack**, name `printask`, **Web editor**, and paste the contents of [`docker-compose.portainer.yml`](../docker-compose.portainer.yml). In **Environment variables** add:

| Variable | Value |
|---|---|
| `NOTION_API_KEY` | your Notion integration token (if using Notion) |
| `NOTION_DATABASE_ID` | your database ID (if using Notion) |
| `SLACK_BOT_TOKEN`, `SLACK_CHANNEL_IDS` | only if using Slack; see "Slack setup" in [README.md](../README.md) |
| `PRINTER_HOST` | the **printer's** IP, e.g. `192.168.1.87` (not `127.0.0.1`, not the NAS) |
| `PRINTER_PORT` | `9100` |
| `ANTHROPIC_API_KEY` | optional for Notion; required for Slack in the default `claude` mode |
| `UI_PORT`, `UI_PASSWORD` | optional web UI, e.g. `8081` and a long password; open `http://<nas-ip>:8081` (see "Web UI" in [README.md](../README.md)). Stays off without the password |

Check that the values saved, then **Deploy the stack**.

The first run prints **every** task in the database whose Status is To Do / Not started and that hasn't printed yet. If your list is long, set most of them to another status first.

### 4. See what it is doing

The compose file sets the `json-file` log driver, so **Containers → printask → Logs** in Portainer works. Without it, Synology's default `db` log driver makes logs come out empty. A good cycle looks like:
```
Fetched 1 To Do tasks from Notion
Printed 'My task' to 192.168.1.87:9100
Cycle done: {'fetched': 1, 'skipped': 0, 'printed': 1, 'failed': 0}
```
To run one cycle by hand and see the output in your terminal (replace the container ID from `sudo docker ps`):
```bash
sudo docker exec -e POLL_INTERVAL_SECONDS=0 <container> python -m simple_printer
```
To see what has been seen and printed: `sudo docker exec <container> python -m simple_printer.history 20`. This lists status (`printed` or `duplicate`), priority, title and page ID. The database is created on first start.

### Updating

1. Rebuild and re-export on your PC (step 1) and `docker load` it on the NAS (step 2).
2. In Portainer open the stack, **Editor**, and **replace its YAML with the current [`docker-compose.portainer.yml`](../docker-compose.portainer.yml)**. The stack holds its own copy of the file, so new settings, the `ports:` line for the web UI and the logging block only appear if you paste it again. Your environment variables stay as they are.
3. Add any new variables you want (see below), then **Update the stack** with re-deploy ticked.

The `printer-data` volume is kept, so history and saved settings survive. Never use "remove volumes" when updating, or everything reprints.

### What to add after upgrading to the version with history, Slack and the web UI

| Variable | Needed? |
|---|---|
| `UI_PORT` (e.g. `8081`) and `UI_PASSWORD` | Optional. Turns on the web UI at `http://<nas-ip>:8081`. Use a long password |
| `SLACK_BOT_TOKEN` and `SLACK_CHANNEL_IDS` | Only for Slack (see "Slack setup" in [README.md](../README.md)). `ANTHROPIC_API_KEY` must also be set |
| `HISTORY_PATH`, `DEDUPE_WINDOW_DAYS`, `SETTINGS_PATH` | No. Defaults are fine; they are already in the stack file |

On the first start after upgrading, the old `/data/printed.json` is imported into the new history database, so already-printed tasks are not printed again. Nothing in your existing Notion setup needs to change.

---

## Route B: DSM 7.2+ with Container Manager (untested)

1. Put the project on the NAS, for example `/volume1/docker/Printask`. The NAS has no `git` by default (install **Git Server** from Package Center, or copy from your PC):
   ```bash
   # run on your Mac, in the folder that contains Printask/
   COPYFILE_DISABLE=1 tar --exclude=.git --exclude=.env -czf - Printask | ssh <user>@<nas-ip> "cd /volume1/docker && tar xzf -"
   ```
2. `cp .env.example .env`, edit it (`vi .env`; `nano` is not installed), and `chmod 600 .env`. Set `PRINTER_HOST` to the printer's IP.
3. In Container Manager, **Project → Create**, point it at the folder, and use the existing `docker-compose.yml`. Or over SSH: `sudo docker compose up -d --build`.
4. For a one-shot test use `sudo docker compose run --rm -e POLL_INTERVAL_SECONDS=0 app` rather than `up`: with `restart: unless-stopped`, a container that exits after one run is restarted repeatedly.

---

## State and permissions

- Every task seen is stored in a SQLite database, `/data/history.db`, on the `printer-data` volume, so restarts don't reprint. To reprint everything, remove the volume (Portainer: **Volumes**, or `docker compose down -v`).
- Upgrading from the version that used `/data/printed.json`: that file is imported into the database on first start, so tasks already printed are not printed again. It is left in place.
- Tasks whose title matches one printed in the last 7 days are skipped as duplicates (`DEDUPE_WINDOW_DAYS`, `0` disables).
- The container runs as uid 1000. A named volume works as is. If you bind-mount a shared folder at `/data`, that folder must be writable by uid 1000 (`sudo chown 1000:1000 <folder>`).

## Troubleshooting

| Symptom | Likely cause / fix |
|---|---|
| `pull access denied for printask` | The image isn't loaded on the NAS (Route A, step 2), or its name/tag differs from `printask:latest` |
| `unknown shorthand flag: 'd' in -d` | The NAS has no Compose v2 (`docker compose`). Use Route A, or update to DSM 7.2+ |
| Container is `Up` but logs are empty | Synology's `db` log driver; use the `logging: json-file` block from the compose file. Run `sudo docker inspect --format '{{.HostConfig.LogConfig.Type}}' <container>` to check |
| `operation not permitted` or container exits immediately | Old Docker engine vs. the newer Debian-based image. Update DSM/Docker, or as a stopgap add `security_opt: [seccomp:unconfined]` to the service |
| `exec format error` | Image built for the wrong CPU; rebuild with `--platform linux/amd64` |
| Socket or `Device not found` errors | `PRINTER_HOST` wrong (e.g. `127.0.0.1`, or the NAS's own IP), printer off, or firewall/VLAN between NAS and printer |
| `Missing required environment variables` or `No task source configured` | `PRINTER_HOST` is empty, or neither Notion nor Slack is fully set (token + IDs) in the stack's environment settings |
| Slack: `not_in_channel` | Invite the bot to the channel: `/invite @your-app-name` |
| Slack: `missing_scope` / `invalid_auth` | Add `channels:history` (and `groups:history` for private channels) and reinstall the app, or the token is wrong |
| All Slack cards are MEDIUM, or the app won't start in Slack mode | No `ANTHROPIC_API_KEY`. See "How priority is decided" in [README.md](../README.md) |
| Slack: messages never print | Check `SLACK_TASK_MODE`: in `claude` mode, chit-chat and notifications are ignored on purpose. `python -m simple_printer.history` shows `ignored` rows |
| `POLL_INTERVAL_SECONDS` shows 300 when you set 0 | The stack's environment value didn't save; re-check and update the stack |
| Web UI doesn't load | Both `UI_PORT` and `UI_PASSWORD` must be set in the stack; the port must match the `ports:` mapping; check DSM's firewall isn't blocking it |
| `Fetched 0 To Do tasks` | Status isn't To Do / Not started, the integration isn't connected to the database, or wrong database ID |
| `Notion API error: 401` / `404` | Wrong token or database ID, or the database isn't shared with the integration |
| `git clone` says "Authentication failed" | GitHub doesn't accept passwords; use a fine-grained token with read-only Contents access for this repo, or copy the files from a PC |
| I want a task to print again, or to wipe the history | Use **Clear history** at the bottom of the web UI's **Tasks** tab, or see "Clearing the history" in [README.md](../README.md). Anything still `To Do` in Notion prints again once cleared |
| `State file ... is corrupt` or `History database ... is unusable` | Delete the named file (or remove the volume); this reprints every open task |
| A task didn't print and the log says "same title as ..." | Duplicate-title skip. Rename the task, wait out the window, or set `DEDUPE_WINDOW_DAYS=0` |
