"""Small password-protected web UI (stdlib only): status, history, settings, test buttons.

Secrets (API keys, tokens) are never shown or editable here. Only the
non-secret settings in ``settings.FIELDS`` can be changed.
"""

import hmac
import html
import logging
import os
import secrets
import threading
import time
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Dict, Optional
from urllib.parse import parse_qs

from . import actions, settings
from .history import History
from .status import Status

logger = logging.getLogger(__name__)

COOKIE = "printask_session"
SESSION_SECONDS = 12 * 3600
MAX_BODY = 16 * 1024
REQUEST_TIMEOUT = 15  # seconds a client may stay silent before the connection is dropped
MAX_FAILED_LOGINS = 5
LOCKOUT_SECONDS = 60

CLEAR_LABELS = {
    "all": "everything",
    "ignored": "ignored messages",
    "duplicate": "duplicates",
    "noise": "ignored messages and duplicates",
    "older": "everything older than",
}

CSS = """
body{font:15px/1.5 system-ui,sans-serif;max-width:980px;margin:2rem auto;padding:0 1rem;color:#1c1c1e}
h1{margin:0 0 .2rem}h2{margin-top:2rem;border-bottom:1px solid #ddd;padding-bottom:.3rem}
table{border-collapse:collapse;width:100%}th,td{text-align:left;padding:.35rem .5rem;border-bottom:1px solid #eee;vertical-align:top}
.muted{color:#6b6b70}.ok{color:#1a7f37}.bad{color:#cf222e}.warn{color:#9a6700}
form.inline{display:inline}label{display:block;margin:.6rem 0 .1rem;font-weight:600}
input[type=text],input[type=password],select{width:100%;max-width:26rem;padding:.35rem;font:inherit}
button{font:inherit;padding:.4rem .9rem;margin:.2rem .3rem .2rem 0;cursor:pointer}
.box{background:#f6f8fa;border:1px solid #ddd;border-radius:6px;padding:.6rem 1rem;margin:.6rem 0}
code{background:#f0f0f0;padding:0 .25rem;border-radius:3px}
nav{display:flex;gap:.3rem;align-items:center;border-bottom:1px solid #ddd;margin-bottom:1rem}
nav a{padding:.5rem .9rem;text-decoration:none;color:#1c1c1e;border-bottom:3px solid transparent}
nav a.on{border-bottom-color:#0969da;font-weight:600}nav form{margin-left:auto}
.filters a{margin-right:.8rem}.filters a.on{font-weight:700}
td.reason{max-width:20rem;color:#6b6b70}
"""


def _page(title: str, body: str) -> bytes:
    return (f"<!doctype html><html><head><meta charset=utf-8><meta name=viewport content='width=device-width'>"
            f"<title>{html.escape(title)}</title><style>{CSS}</style></head><body>{body}</body></html>").encode()


def _ago(ts: Optional[float]) -> str:
    if not ts:
        return "never"
    seconds = int(time.time() - ts)
    if seconds < 90:
        return f"{seconds}s ago"
    return f"{seconds // 60}m ago" if seconds < 5400 else f"{seconds // 3600}h ago"


class Sessions:
    """In-memory sessions, each with its own CSRF token."""

    def __init__(self):
        self._lock = threading.Lock()
        self._sessions: Dict[str, Dict] = {}
        self._failures = 0
        self._locked_until = 0.0

    def locked(self) -> bool:
        return time.time() < self._locked_until

    def failed(self) -> None:
        with self._lock:
            self._failures += 1
            if self._failures >= MAX_FAILED_LOGINS:
                self._locked_until, self._failures = time.time() + LOCKOUT_SECONDS, 0

    def create(self) -> Dict:
        with self._lock:
            self._failures = 0
            sid = secrets.token_urlsafe(32)
            self._sessions[sid] = {"sid": sid, "csrf": secrets.token_urlsafe(24),
                                   "expires": time.time() + SESSION_SECONDS}
            return self._sessions[sid]

    def get(self, sid: Optional[str]) -> Optional[Dict]:
        with self._lock:
            session = self._sessions.get(sid or "")
            if session and session["expires"] > time.time():
                return session
            self._sessions.pop(sid or "", None)
            return None

    def delete(self, sid: Optional[str]) -> None:
        with self._lock:
            self._sessions.pop(sid or "", None)


def start(port: int, password: str, history_path: str, status: Status) -> ThreadingHTTPServer:
    sessions = Sessions()

    def history() -> History:
        # The poll loop owns its own connection; each request opens a short-lived one.
        return History(history_path)

    class Handler(BaseHTTPRequestHandler):
        server_version = "Printask"
        sys_version = ""
        timeout = REQUEST_TIMEOUT  # drop idle or stalled connections instead of holding a thread forever

        # ---- helpers -------------------------------------------------------
        def log_message(self, *args):
            pass

        def _security_headers(self):
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Content-Security-Policy",
                             "default-src 'none'; style-src 'unsafe-inline'; form-action 'self'; base-uri 'none'")

        def _send(self, code: int, body: bytes, content_type="text/html; charset=utf-8", headers=None):
            self.send_response(code)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self._security_headers()
            for key, value in (headers or []):
                self.send_header(key, value)
            self.end_headers()
            self.wfile.write(body)

        def _redirect(self, where: str, headers=None):
            self.send_response(303)
            self.send_header("Location", where)
            self.send_header("Content-Length", "0")
            self._security_headers()
            for key, value in (headers or []):
                self.send_header(key, value)
            self.end_headers()

        def _session(self) -> Optional[Dict]:
            cookie = SimpleCookie(self.headers.get("Cookie", ""))
            return sessions.get(cookie[COOKIE].value if COOKIE in cookie else None)

        def _form(self) -> Dict[str, str]:
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                raise ValueError("bad request") from None
            if length < 0 or length > MAX_BODY:  # a negative length would block reading for a body forever
                raise ValueError("bad request")
            data = parse_qs(self.rfile.read(length).decode("utf-8", "replace"), keep_blank_values=True)
            return {key: values[0] for key, values in data.items()}

        # ---- routes --------------------------------------------------------
        def do_GET(self):
            path = self.path.split("?")[0]
            if path == "/login":
                return self._send(200, self._login_page())
            session = self._session()
            if not session:
                return self._redirect("/login")
            if path == "/":
                return self._send(200, self._dashboard(session))
            if path == "/tasks":
                query = parse_qs(self.path.partition("?")[2])
                return self._send(200, self._tasks_page(session, query.get("status", [""])[0]))
            if path == "/settings":
                return self._send(200, self._settings_page(session))
            self._send(404, _page("Not found", "<p>Not found. <a href='/'>Back</a></p>"))

        def do_POST(self):
            path = self.path.split("?")[0]
            try:
                form = self._form()
            except ValueError:
                return self._send(400, _page("Bad request", "<p>Bad or too large request.</p>"))
            if path == "/login":
                return self._login(form)
            session = self._session()
            if not session:
                return self._redirect("/login")
            if not hmac.compare_digest(form.get("csrf", ""), session["csrf"]):
                return self._send(403, _page("Forbidden", "<p>Invalid form token. Reload the page.</p>"))
            routes = {"/logout": self._logout, "/settings": self._save_settings, "/run": self._run_now,
                      "/tasks/clear": self._clear_review, "/tasks/clear/confirm": self._clear_confirm,
                      "/test-print": self._test_print, "/preview-slack": self._preview_slack}
            handler = routes.get(path)
            if not handler:
                return self._send(404, _page("Not found", "<p>Not found.</p>"))
            handler(session, form)

        # ---- actions -------------------------------------------------------
        def _login(self, form):
            if sessions.locked():
                return self._send(429, self._login_page("Too many attempts. Wait a minute."))
            if hmac.compare_digest(form.get("password", "").encode(), password.encode()):
                session = sessions.create()
                cookie = (f"{COOKIE}={session['sid']}; HttpOnly; SameSite=Strict; Path=/; "
                          f"Max-Age={SESSION_SECONDS}")
                return self._redirect("/", [("Set-Cookie", cookie)])
            sessions.failed()
            time.sleep(1)
            self._send(401, self._login_page("Wrong password."))

        def _logout(self, session, form):
            sessions.delete(session["sid"])
            self._redirect("/login", [("Set-Cookie", f"{COOKIE}=; Max-Age=0; Path=/")])

        def _save_settings(self, session, form):
            store = settings.current()
            try:
                errors = (store.update({k: v for k, v in form.items() if k in settings.FIELDS})
                          if store else ["Settings are not available"])
            except OSError as exc:
                logger.exception("Could not save settings")
                return self._result(session, "Settings not saved",
                                    f"Could not write {store.path}: {exc.strerror or exc}. "
                                    "Set SETTINGS_PATH to a writable location.", ok=False, back="/settings")
            if errors:
                body = "".join(f"<li class=bad>{html.escape(e)}</li>" for e in errors)
                return self._send(400, _page("Settings", f"<h1>Settings not saved</h1><ul>{body}</ul>"
                                                         "<p><a href='/settings'>Back to settings</a></p>"))
            self._result(session, "Settings saved", "They apply on the next poll.", back="/settings")

        def _run_now(self, session, form):
            status.wake.set()
            self._result(session, "Poll started", "A poll was requested; it runs now and prints any new tasks. "
                                                  "Refresh the dashboard in a few seconds.")

        def _test_print(self, session, form):
            try:
                self._result(session, "Test card", actions.print_test_card())
            except Exception as exc:  # noqa: BLE001
                logger.exception("Test print failed")
                self._result(session, "Test card failed", f"{type(exc).__name__}: {exc}", ok=False)

        def _preview_slack(self, session, form):
            try:
                h = history()
                try:
                    rows = actions.preview_slack(h)
                finally:
                    h.close()
            except Exception as exc:  # noqa: BLE001
                logger.exception("Slack preview failed")
                return self._result(session, "Slack preview failed", f"{type(exc).__name__}: {exc}", ok=False)
            if not rows:
                return self._result(session, "Slack preview", "No messages in the lookback window.")
            body = "".join(
                f"<tr><td>{html.escape(r['text'])}</td><td>{html.escape(r['decision'])}</td>"
                f"<td>{html.escape(r['title'])}</td><td>{html.escape(r['priority'])}</td></tr>" for r in rows)
            self._send(200, _page("Slack preview",
                                  "<h1>Slack preview</h1><p class=muted>Nothing was printed or recorded.</p>"
                                  "<table><tr><th>Message</th><th>Decision</th><th>Card title</th><th>Priority</th></tr>"
                                  f"{body}</table><p><a href='/'>Back</a></p>"))

        def _result(self, session, title: str, message: str, ok: bool = True, back: str = "/"):
            css = "ok" if ok else "bad"
            self._send(200 if ok else 500, _page(title, f"<h1>{html.escape(title)}</h1>"
                                                        f"<p class={css}>{html.escape(message)}</p>"
                                                        f"<p><a href='{back}'>Back</a></p>"))

        def _clear_params(self, form):
            scope = form.get("scope", "")
            if scope not in History.CLEAR_SCOPES:
                raise ValueError("Choose what to clear.")
            days = None
            if scope == "older":
                try:
                    days = int(form.get("days", ""))
                except ValueError:
                    raise ValueError("Enter a whole number of days.") from None
                if not 1 <= days <= 3650:
                    raise ValueError("Days must be between 1 and 3650.")
            return scope, days

        def _clear_review(self, session, form):
            try:
                scope, days = self._clear_params(form)
            except ValueError as exc:
                return self._send(400, _page("Clear history", f"<h1>Clear history</h1><p class=bad>{html.escape(str(exc))}</p>"
                                                              "<p><a href='/tasks'>Back to tasks</a></p>"))
            h = history()
            try:
                count = h.count_for_clear(scope, days)
            finally:
                h.close()
            what = CLEAR_LABELS[scope] + (f" ({days} days)" if scope == "older" else "")
            if count == 0:
                body = f"<p>Nothing matches <b>{html.escape(what)}</b>. Nothing to clear.</p><p><a href='/tasks'>Back to tasks</a></p>"
            else:
                hidden = (f"{self._csrf(session)}<input type=hidden name=scope value='{html.escape(scope, quote=True)}'>"
                          f"<input type=hidden name=days value='{days if days else ''}'>")
                body = (f"<p>This will permanently delete <b>{count}</b> row(s): <b>{html.escape(what)}</b>.</p>"
                        "<div class=box>Printask forgets what it deletes. Notion tasks that are still <code>To Do</code> and "
                        "Slack messages inside the lookback window are handled again and <b>may print again</b> "
                        "(Slack messages are re-checked by Claude, which uses API calls).</div>"
                        f"<form method=post action=/tasks/clear/confirm>{hidden}"
                        "<button>Yes, delete them</button> <a href='/tasks'>Cancel</a></form>")
            self._send(200, _page("Clear history", f"<h1>Clear history</h1>{body}"))

        def _clear_confirm(self, session, form):
            try:
                scope, days = self._clear_params(form)
            except ValueError as exc:
                return self._send(400, _page("Clear history", f"<h1>Clear history</h1><p class=bad>{html.escape(str(exc))}</p>"
                                                              "<p><a href='/tasks'>Back to tasks</a></p>"))
            h = history()
            try:
                deleted = h.clear(scope, days)
            finally:
                h.close()
            self._result(session, "History cleared", f"Deleted {deleted} row(s).", back="/tasks")

        # ---- pages ---------------------------------------------------------
        def _login_page(self, error: str = "") -> bytes:
            err = f"<p class=bad>{html.escape(error)}</p>" if error else ""
            return _page("Printask login", f"<h1>Printask</h1>{err}<form method=post action=/login>"
                                           "<label>Password</label><input type=password name=password autofocus>"
                                           "<p><button>Log in</button></p></form>")

        def _csrf(self, session) -> str:
            return f"<input type=hidden name=csrf value='{html.escape(session['csrf'])}'>"

        def _layout(self, session, active: str, title: str, body: str) -> bytes:
            tabs = "".join(f"<a href='{href}'{' class=on' if key == active else ''}>{label}</a>"
                           for key, href, label in (("status", "/", "Dashboard"),
                                                    ("tasks", "/tasks", "Tasks"),
                                                    ("settings", "/settings", "Settings")))
            nav = (f"<nav>{tabs}<form method=post action=/logout>{self._csrf(session)}"
                   "<button>Log out</button></form></nav>")
            return _page(f"Printask - {title}", f"<h1>Printask</h1>{nav}{body}")

        def _dashboard(self, session) -> bytes:
            snap = status.snapshot()
            csrf = self._csrf(session)
            st = snap["last_stats"]
            if snap["last_error"]:
                last = f"<span class=bad>error {_ago(snap['last_cycle_at'])}: {html.escape(snap['last_error'])}</span>"
            elif snap["last_cycle_at"]:
                last = (f"<span class=ok>ok {_ago(snap['last_cycle_at'])}</span> &middot; " +
                        ", ".join(f"{k}: {v}" for k, v in st.items()))
            else:
                last = "<span class=muted>no poll yet</span>"
            conf = lambda name: "<span class=ok>configured</span>" if os.getenv(name) else "<span class=muted>not set</span>"
            sources = ", ".join(snap["sources"]) or "none"
            return self._layout(session, "status", "Dashboard", f"""
<div class=box>Sources running: <b>{html.escape(sources)}</b><br>Last poll: {last}<br>
<span class=muted>Printer {html.escape(os.getenv('PRINTER_HOST', '?'))}:{html.escape(os.getenv('PRINTER_PORT', '9100'))}
&middot; Notion {conf('NOTION_API_KEY')} &middot; Slack token {conf('SLACK_BOT_TOKEN')}
&middot; Anthropic key {conf('ANTHROPIC_API_KEY')}</span></div>
<form class=inline method=post action=/run>{csrf}<button>Poll now</button></form>
<form class=inline method=post action=/test-print>{csrf}<button>Print test card</button></form>
<form class=inline method=post action=/preview-slack>{csrf}<button>Preview Slack (uses Claude)</button></form>
<p class=muted>Poll now runs a poll immediately and prints any new tasks. Preview Slack prints and records nothing.
See <a href='/tasks'>Tasks</a> for what has been handled and <a href='/settings'>Settings</a> to change behavior.</p>
""")

        def _tasks_page(self, session, status_filter: str) -> bytes:
            status_filter = status_filter if status_filter in History.STATUSES else ""
            h = history()
            try:
                rows = h.recent_detailed(100, status_filter or None)
            finally:
                h.close()
            filters = "".join(
                f"<a href='/tasks{('?status=' + key) if key else ''}'{' class=on' if key == status_filter else ''}>{label}</a>"
                for key, label in (("", "All"), ("printed", "Printed"), ("duplicate", "Duplicates"),
                                   ("ignored", "Ignored")))

            def title_cell(r):
                text = html.escape(r["title"] or "(imported)")
                url = r["url"] or ""
                # only link real web URLs (never javascript: or data:)
                if url.startswith(("https://", "http://")):
                    return f"<a href='{html.escape(url, quote=True)}' rel='noopener noreferrer'>{text}</a>"
                return text

            body = "".join(
                f"<tr><td class=muted>{html.escape(r['created_at'][:19].replace('T', ' '))}</td>"
                f"<td>{html.escape(r['source'])}</td><td>{html.escape(r['status'])}</td>"
                f"<td>{html.escape(r['priority'] or '')}</td><td>{title_cell(r)}</td>"
                f"<td class=reason>{html.escape((r['reason'] or '')[:160])}</td></tr>" for r in rows
            ) or "<tr><td colspan=6 class=muted>Nothing yet</td></tr>"
            return self._layout(session, "tasks", "Tasks", f"""
<p class=filters>{filters} <span class=muted>(latest 100, times in UTC)</span></p>
<table><tr><th>When</th><th>Source</th><th>Status</th><th>Priority</th><th>Title</th><th>Reason</th></tr>{body}</table>
<h2>Clear history</h2>
<form method=post action=/tasks/clear>{self._csrf(session)}
<label>What to clear</label>
<select name=scope>
<option value=noise>Ignored messages and duplicates (safest)</option>
<option value=ignored>Ignored messages only</option>
<option value=duplicate>Duplicates only</option>
<option value=older>Everything older than N days</option>
<option value=all>Everything</option>
</select>
<label>Days (only for "older than")</label><input type=text name=days size=5 value=30>
<p><button>Review...</button> <span class=muted>You will see how many rows are affected before anything is deleted.</span></p>
</form>
""")

        def _settings_page(self, session) -> bytes:
            store = settings.current()
            overrides = store.overrides() if store else {}
            fields = ""
            for name, (kind, constraint, default, label) in settings.FIELDS.items():
                current = settings.get(name)
                note = "set here" if name in overrides else f"from environment/default ({html.escape(default or 'empty')})"
                if kind == "choice":
                    opts = "".join(f"<option{' selected' if o == current else ''}>{o}</option>" for o in constraint)
                    control = f"<select name={name}>{opts}</select>"
                else:
                    control = f"<input type=text name={name} value='{html.escape(current, quote=True)}'>"
                fields += f"<label>{html.escape(label)}</label>{control} <span class=muted>{note}</span>"
            return self._layout(session, "settings", "Settings", f"""
<p class=muted>Tokens and API keys are set in the environment (Portainer) and cannot be changed here.
Leave a field blank to go back to the environment value. Changes apply on the next poll.</p>
<form method=post action=/settings>{self._csrf(session)}{fields}<p><button>Save settings</button></p></form>
""")

    server = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.1}, daemon=True).start()
    return server
