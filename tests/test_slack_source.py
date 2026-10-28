import pytest

from simple_printer import slack_source
from simple_printer.slack_source import clean_text, fetch_messages, provisional_title

NOW = 1_700_000_000.0


class FakeResponse:
    def __init__(self, payload, status_code=200, headers=None):
        self._payload, self.status_code, self.headers = payload, status_code, headers or {}

    def json(self):
        return self._payload


@pytest.fixture
def slack(monkeypatch):
    """Route httpx.get to canned Slack API responses; record the calls."""
    calls = []
    state = {"history": {}, "auth": {"ok": True, "url": "https://acme.slack.com/", "user_id": "UBOT"}}

    def get(url, params=None, headers=None, timeout=None):
        method = url.rsplit("/", 1)[1]
        calls.append((method, params, headers))
        if method == "auth.test":
            return FakeResponse(state["auth"])
        value = state["history"][params["channel"]]
        if isinstance(value, list):  # successive pages
            return FakeResponse(value.pop(0))
        return FakeResponse(value)

    monkeypatch.setattr(slack_source.httpx, "get", get)
    return calls, state


def msgs(*items, more=False, cursor=None):
    out = {"ok": True, "messages": list(items), "has_more": more}
    if cursor:
        out["response_metadata"] = {"next_cursor": cursor}
    return out


def test_clean_text():
    raw = "Hi <@U123> see <https://x.io/t/1|ticket 1> and <https://y.io> in <#C1|support> &amp; <!here>"
    assert clean_text(raw) == "Hi @U123 see ticket 1 (https://x.io/t/1) and https://y.io in #support &"


def test_provisional_title_first_line_and_truncation():
    assert provisional_title("first line\nsecond") == "first line"
    assert len(provisional_title("y" * 300)) == 80


def test_fetch_basic_shape_and_url(slack):
    calls, state = slack
    state["history"]["C1"] = msgs({"ts": "1699999000.000100", "user": "U1", "text": "Printer on fire"})
    (m,) = fetch_messages("xoxb-1", ["C1"], now=NOW)
    assert m.id == "slack:C1:1699999000.000100" and m.title == "Printer on fire"
    assert m.url == "https://acme.slack.com/archives/C1/p1699999000000100"
    assert m.source == "slack" and not m.is_bot
    method, params, headers = calls[1]
    assert headers["Authorization"] == "Bearer xoxb-1"
    assert float(params["oldest"]) == pytest.approx(NOW - 24 * 3600)


def test_fetch_filters_noise(slack):
    _, state = slack
    state["history"]["C1"] = msgs(
        {"ts": "1.0", "subtype": "channel_join", "user": "U1", "text": "joined"},
        {"ts": "2.0", "user": "UBOT", "text": "my own message"},
        {"ts": "3.0", "user": "U1", "text": ""},
        {"ts": "4.0", "user": "U1", "text": "keep me"},
    )
    assert [m.text for m in fetch_messages("t", ["C1"], now=NOW)] == ["keep me"]


def test_bot_messages_and_attachment_only_posts(slack):
    _, state = slack
    state["history"]["C1"] = msgs(
        {"ts": "1.0", "user": "U1", "text": "human talk"},
        {"ts": "2.0", "bot_id": "B1", "subtype": "bot_message", "text": "",
         "attachments": [{"title": "Ticket #42: login broken", "text": "Customer cannot sign in"}]},
    )
    (m,) = fetch_messages("t", ["C1"], mode="bots", now=NOW)
    assert m.is_bot and "Ticket #42" in m.text and "cannot sign in" in m.text


def test_keyword_mode(slack):
    _, state = slack
    state["history"]["C1"] = msgs({"ts": "1.0", "user": "U1", "text": "lunch?"},
                                  {"ts": "2.0", "user": "U1", "text": "TODO: renew certs"})
    assert [m.text for m in fetch_messages("t", ["C1"], mode="keyword", keyword="todo:", now=NOW)] == ["TODO: renew certs"]


def test_pagination_and_multiple_channels_sorted_oldest_first(slack):
    _, state = slack
    state["history"]["C1"] = [msgs({"ts": "5.0", "user": "U1", "text": "c1 late"}, more=True, cursor="next"),
                              msgs({"ts": "1.0", "user": "U1", "text": "c1 early"})]
    state["history"]["C2"] = msgs({"ts": "3.0", "user": "U2", "text": "c2 mid"})
    out = fetch_messages("t", ["C1", "C2"], now=NOW)
    assert [m.text for m in out] == ["c1 early", "c2 mid", "c1 late"]


def test_invalid_mode_and_missing_keyword():
    with pytest.raises(ValueError, match="SLACK_TASK_MODE"):
        fetch_messages("t", ["C1"], mode="weird")
    with pytest.raises(ValueError, match="SLACK_KEYWORD"):
        fetch_messages("t", ["C1"], mode="keyword")


def test_not_in_channel_error_has_hint(slack):
    _, state = slack
    state["history"]["C1"] = {"ok": False, "error": "not_in_channel"}
    with pytest.raises(RuntimeError, match="invite the bot"):
        fetch_messages("t", ["C1"], now=NOW)


def test_rate_limit_and_http_errors(monkeypatch):
    monkeypatch.setattr(slack_source.httpx, "get",
                        lambda *a, **k: FakeResponse({}, status_code=429, headers={"Retry-After": "30"}))
    with pytest.raises(RuntimeError, match="rate limit.*30"):
        fetch_messages("t", ["C1"], now=NOW)
    monkeypatch.setattr(slack_source.httpx, "get", lambda *a, **k: FakeResponse({}, status_code=500))
    with pytest.raises(RuntimeError, match="500"):
        fetch_messages("t", ["C1"], now=NOW)
