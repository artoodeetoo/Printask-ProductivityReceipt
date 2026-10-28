import http.client
import re
import threading
from urllib.parse import urlencode

import pytest

from simple_printer import actions, settings, webui
from simple_printer.history import History
from simple_printer.notion_source import NotionTask
from simple_printer.priority import PriorityResult
from simple_printer.status import Status

PASSWORD = "s3cret-pass"


@pytest.fixture
def ui(tmp_path, monkeypatch):
    monkeypatch.setattr(webui.time, "sleep", lambda s: None)  # no 1s delay on bad logins
    for name in ("SLACK_BOT_TOKEN", "NOTION_API_KEY", "ANTHROPIC_API_KEY"):
        monkeypatch.setenv(name, "super-secret-token-value")
    monkeypatch.setenv("PRINTER_HOST", "192.168.1.87")
    path = str(tmp_path / "h.db")
    history = History(path)
    history.record_printed(NotionTask("p1", "<script>alert(1)</script> Pay rent", "Low", None, "u"),
                           PriorityResult("HIGH", "r"))
    history.close()
    settings.configure(str(tmp_path / "s.json"))
    status = Status()
    status.set_sources(["notion", "slack"])
    server = webui.start(0, PASSWORD, path, status)
    port = server.server_address[1]

    class Client:
        cookie = ""

        def request(self, method, path, form=None, cookie=True):
            conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
            headers = {"Cookie": self.cookie} if cookie and self.cookie else {}
            body = urlencode(form).encode() if form is not None else None
            if body:
                headers["Content-Type"] = "application/x-www-form-urlencoded"
            conn.request(method, path, body=body, headers=headers)
            response = conn.getresponse()
            text = response.read().decode()
            if response.getheader("Set-Cookie"):
                self.cookie = response.getheader("Set-Cookie").split(";")[0]
            return response.status, response, text

        def login(self):
            code, _, _ = self.request("POST", "/login", {"password": PASSWORD})
            assert code == 303

        def csrf(self):
            _, _, page = self.request("GET", "/")
            return re.search(r"name=csrf value='([^']+)'", page).group(1)

    client = Client()
    client.status, client.port, client.history_path = status, port, path
    yield client
    server.shutdown()
    settings.configure(None)


def test_requires_login(ui):
    code, response, _ = ui.request("GET", "/")
    assert code == 303 and response.getheader("Location") == "/login"
    code, _, _ = ui.request("POST", "/run", {"csrf": "x"})
    assert code == 303  # not logged in: redirected, nothing executed
    assert not ui.status.wake.is_set()


def test_wrong_password_and_lockout(ui):
    for _ in range(webui.MAX_FAILED_LOGINS):
        code, _, page = ui.request("POST", "/login", {"password": "nope"})
        assert code == 401 and "Wrong password" in page
    code, _, page = ui.request("POST", "/login", {"password": PASSWORD})
    assert code == 429  # locked out even with the right password


def test_login_sets_hardened_cookie(ui):
    code, response, _ = ui.request("POST", "/login", {"password": PASSWORD})
    cookie = response.getheader("Set-Cookie")
    assert code == 303 and "HttpOnly" in cookie and "SameSite=Strict" in cookie
    assert response.getheader("Content-Security-Policy") and response.getheader("X-Frame-Options") == "DENY"


def test_dashboard_is_status_and_buttons_only(ui):
    ui.login()
    code, _, page = ui.request("GET", "/")
    assert code == 200
    assert "notion, slack" in page and "192.168.1.87" in page and "configured" in page
    for button in ("Poll now", "Print test card", "Preview Slack"):
        assert button in page
    assert "Pay rent" not in page and "<table" not in page  # tasks moved to their own page
    assert "Save settings" not in page                       # settings moved to their own page


def test_tasks_page_escapes_html_and_hides_secrets(ui):
    ui.login()
    code, _, page = ui.request("GET", "/tasks")
    assert code == 200
    assert "<script>alert(1)</script>" not in page and "&lt;script&gt;" in page and "Pay rent" in page
    assert "super-secret-token-value" not in page and PASSWORD not in page


def test_settings_page_has_form_and_hides_secrets(ui):
    ui.login()
    code, _, page = ui.request("GET", "/settings")
    assert code == 200 and "Save settings" in page and "POLL_INTERVAL_SECONDS" in page
    assert "super-secret-token-value" not in page and PASSWORD not in page
    assert "Pay rent" not in page


def test_every_page_has_nav_with_active_tab(ui):
    ui.login()
    for path, label in (("/", "Dashboard"), ("/tasks", "Tasks"), ("/settings", "Settings")):
        _, _, page = ui.request("GET", path)
        nav = page[page.index("<nav>"):page.index("</nav>")]
        assert f"<a href='{path}' class=on>{label}</a>" in nav
        assert nav.count("class=on>") == 1 and "Log out" in nav


def test_new_pages_require_login(ui):
    for path in ("/tasks", "/settings"):
        code, response, _ = ui.request("GET", path)
        assert code == 303 and response.getheader("Location") == "/login"


def test_tasks_status_filter(ui):
    ui.login()
    h = History(ui.history_path)
    h.record_duplicate(NotionTask("d1", "A duplicate one", None, None, "https://notion.so/d1"), "p1")
    h.record_ignored(NotionTask("i1", "Chit chat", None, None, ""), "not a task")
    h.close()
    _, _, page = ui.request("GET", "/tasks?status=duplicate")
    assert "A duplicate one" in page and "Pay rent" not in page and "Chit chat" not in page
    _, _, page = ui.request("GET", "/tasks?status=ignored")
    assert "Chit chat" in page and "A duplicate one" not in page
    _, _, page = ui.request("GET", "/tasks?status=printed")
    assert "Pay rent" in page and "Chit chat" not in page
    _, _, page = ui.request("GET", "/tasks?status=bogus%27%3Bdrop")  # unknown filter -> shows all
    assert "Pay rent" in page and "Chit chat" in page


def test_task_links_only_for_http_urls(ui):
    ui.login()
    h = History(ui.history_path)
    h.record_printed(NotionTask("x1", "Evil link", None, None, "javascript:alert(1)"), PriorityResult("LOW", "r"))
    h.record_printed(NotionTask("x2", "Good link", None, None, "https://acme.slack.com/archives/C1/p1"),
                     PriorityResult("LOW", "r"))
    h.close()
    _, _, page = ui.request("GET", "/tasks")
    assert "javascript:" not in page
    assert "href='https://acme.slack.com/archives/C1/p1'" in page and ">Good link</a>" in page


def test_tasks_page_shows_claude_reason(ui):
    ui.login()
    _, _, page = ui.request("GET", "/tasks")
    assert "<th>Reason</th>" in page and ">r<" in page


def test_post_requires_csrf_token(ui):
    ui.login()
    code, _, _ = ui.request("POST", "/run", {"csrf": "wrong"})
    assert code == 403 and not ui.status.wake.is_set()
    code, _, _ = ui.request("POST", "/run", {})
    assert code == 403


def test_poll_now_wakes_the_loop(ui):
    ui.login()
    code, _, page = ui.request("POST", "/run", {"csrf": ui.csrf()})
    assert code == 200 and ui.status.wake.is_set()


def test_save_settings_valid_and_invalid(ui):
    ui.login()
    csrf = ui.csrf()
    code, _, _ = ui.request("POST", "/settings", {"csrf": csrf, "POLL_INTERVAL_SECONDS": "90",
                                                   "SLACK_TASK_MODE": "bots"})
    assert code == 200 and settings.get("POLL_INTERVAL_SECONDS") == "90" and settings.get("SLACK_TASK_MODE") == "bots"
    code, _, page = ui.request("POST", "/settings", {"csrf": csrf, "POLL_INTERVAL_SECONDS": "1"})
    assert code == 400 and "between 30 and 86400" in page
    assert settings.get("POLL_INTERVAL_SECONDS") == "90"  # unchanged


def test_settings_endpoint_ignores_secrets(ui):
    ui.login()
    ui.request("POST", "/settings", {"csrf": ui.csrf(), "SLACK_BOT_TOKEN": "xoxb-evil",
                                      "POLL_INTERVAL_SECONDS": "60"})
    assert "SLACK_BOT_TOKEN" not in settings.current().overrides()


def test_test_print_success_and_failure(ui, monkeypatch):
    ui.login()
    monkeypatch.setattr(actions.printing, "print_task", lambda t, p: None)
    code, _, page = ui.request("POST", "/test-print", {"csrf": ui.csrf()})
    assert code == 200 and "Test card sent" in page
    def boom(t, p):
        raise OSError("printer offline")
    monkeypatch.setattr(actions.printing, "print_task", boom)
    code, _, page = ui.request("POST", "/test-print", {"csrf": ui.csrf()})
    assert code == 500 and "printer offline" in page


def test_preview_slack_does_not_write_history(ui, monkeypatch):
    ui.login()
    from simple_printer.slack_source import SlackMessage
    msgs = [SlackMessage(id="slack:C1:1", title="t", text="login <b>broken</b>", channel="C1", ts="1", url="u", is_bot=False)]
    monkeypatch.setattr(actions.slack_source, "fetch_messages", lambda *a, **k: msgs)
    monkeypatch.setattr(actions.priority, "extract_task",
                        lambda text: actions.priority.ExtractedTask("Fix login", "HIGH", "r", None))
    monkeypatch.setenv("SLACK_CHANNEL_IDS", "C1ABC")
    code, _, page = ui.request("POST", "/preview-slack", {"csrf": ui.csrf()})
    assert code == 200 and "Fix login" in page and "&lt;b&gt;broken" in page
    h = History(ui.history_path)
    assert not h.has_seen("slack:C1:1")
    h.close()


def test_logout_invalidates_session(ui):
    ui.login()
    ui.request("POST", "/logout", {"csrf": ui.csrf()})
    code, _, _ = ui.request("GET", "/")
    assert code == 303


# ---- clear history -----------------------------------------------------------

def _seed_noise(ui):
    h = History(ui.history_path)
    h.record_duplicate(NotionTask("d1", "A duplicate", None, None, "u"), "p1")
    h.record_ignored(NotionTask("i1", "Chit chat", None, None, ""), "not a task")
    h.close()


def _ids(ui):
    h = History(ui.history_path)
    try:
        return {r["page_id"] for r in h.recent_detailed(100)}
    finally:
        h.close()


def test_tasks_page_has_clear_form(ui):
    ui.login()
    _, _, page = ui.request("GET", "/tasks")
    assert "Clear history" in page and "action=/tasks/clear>" in page and "Ignored messages and duplicates" in page


def test_review_shows_count_and_deletes_nothing(ui):
    ui.login()
    _seed_noise(ui)
    code, _, page = ui.request("POST", "/tasks/clear", {"csrf": ui.csrf(), "scope": "noise"})
    assert code == 200 and "<b>2</b> row(s)" in page and "may print again" in page and "Yes, delete them" in page
    assert _ids(ui) == {"p1", "d1", "i1"}


def test_confirm_deletes_only_the_chosen_scope(ui):
    ui.login()
    _seed_noise(ui)
    code, _, page = ui.request("POST", "/tasks/clear/confirm", {"csrf": ui.csrf(), "scope": "ignored", "days": ""})
    assert code == 200 and "Deleted 1 row(s)" in page
    assert _ids(ui) == {"p1", "d1"}
    ui.request("POST", "/tasks/clear/confirm", {"csrf": ui.csrf(), "scope": "all", "days": ""})
    assert _ids(ui) == set()
    _, _, page = ui.request("GET", "/tasks")
    assert "Nothing yet" in page


def test_review_with_nothing_to_clear_has_no_confirm_button(ui):
    ui.login()
    code, _, page = ui.request("POST", "/tasks/clear", {"csrf": ui.csrf(), "scope": "ignored"})
    assert code == 200 and "Nothing to clear" in page and "Yes, delete them" not in page


@pytest.mark.parametrize("form", [
    {"scope": "bogus"}, {"scope": ""}, {"scope": "older", "days": "0"},
    {"scope": "older", "days": "abc"}, {"scope": "older", "days": "99999"}, {"scope": "all; DROP TABLE tasks"},
])
def test_clear_rejects_bad_input_on_both_steps(ui, form):
    ui.login()
    for route in ("/tasks/clear", "/tasks/clear/confirm"):
        code, _, _ = ui.request("POST", route, {"csrf": ui.csrf(), **form})
        assert code == 400
    assert _ids(ui) == {"p1"}  # untouched


def test_clear_requires_login_and_csrf(ui):
    code, response, _ = ui.request("POST", "/tasks/clear/confirm", {"csrf": "x", "scope": "all"})
    assert code == 303 and _ids(ui) == {"p1"}                       # not logged in
    ui.login()
    code, _, _ = ui.request("POST", "/tasks/clear/confirm", {"csrf": "wrong", "scope": "all"})
    assert code == 403 and _ids(ui) == {"p1"}                       # bad token
    code, _, _ = ui.request("POST", "/tasks/clear/confirm", {"scope": "all"})
    assert code == 403 and _ids(ui) == {"p1"}                       # no token


def test_older_than_days_through_the_ui(ui):
    ui.login()
    code, _, page = ui.request("POST", "/tasks/clear", {"csrf": ui.csrf(), "scope": "older", "days": "30"})
    assert code == 200 and "Nothing to clear" in page            # the seeded row is brand new
    assert _ids(ui) == {"p1"}


def test_unwritable_settings_path_shows_an_error_not_a_crash(ui, monkeypatch):
    ui.login()

    def deny(self, submitted):
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr(settings.Settings, "update", deny)
    code, _, page = ui.request("POST", "/settings", {"csrf": ui.csrf(), "POLL_INTERVAL_SECONDS": "60"})
    assert code == 500 and "Could not write" in page and "SETTINGS_PATH" in page
    code, _, _ = ui.request("GET", "/settings")   # the server is still alive
    assert code == 200


# ---- hardening --------------------------------------------------------------

def _raw(port, request: bytes, wait=3.0):
    import socket
    import time
    s = socket.create_connection(("127.0.0.1", port), timeout=wait)
    s.sendall(request)
    t0 = time.time()
    try:
        data = s.recv(4096)
        return data.split(b"\r\n")[0].decode(errors="replace"), time.time() - t0
    except socket.timeout:
        return "NO RESPONSE", time.time() - t0
    finally:
        s.close()


@pytest.mark.parametrize("length", ["-1", "-999999", "abc", "999999999"])
def test_bad_content_length_is_rejected_quickly(ui, length):
    # a negative length used to block the handler thread waiting for a body forever
    status_line, elapsed = _raw(ui.port, f"POST /login HTTP/1.1\r\nHost: x\r\nContent-Length: {length}\r\n\r\n".encode())
    assert status_line.endswith("400 Bad Request") or " 400 " in status_line
    assert elapsed < 1.5


def test_idle_connections_are_dropped(tmp_path, monkeypatch):
    import socket
    import time
    monkeypatch.setattr(webui, "REQUEST_TIMEOUT", 0.5)
    History(str(tmp_path / "h.db")).close()
    settings.configure(str(tmp_path / "s.json"))
    server = webui.start(0, "pw", str(tmp_path / "h.db"), Status())
    try:
        s = socket.create_connection(("127.0.0.1", server.server_address[1]), timeout=5)
        t0 = time.time()
        assert s.recv(1) == b""            # the server closed the silent connection
        assert time.time() - t0 < 3
        s.close()
    finally:
        server.shutdown()
        settings.configure(None)


def test_history_rejects_unknown_status_even_without_asserts(hist=None):
    import tempfile
    h = History(tempfile.mkdtemp() + "/h.db")
    try:
        with pytest.raises(ValueError):
            h._record(NotionTask("x", "t", None, None, "u"), "bogus")
    finally:
        h.close()
