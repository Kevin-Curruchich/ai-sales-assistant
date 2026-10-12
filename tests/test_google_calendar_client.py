from types import SimpleNamespace

import pytest

from app.services.google_calendar import CalendarError, GoogleCalendarClient


class FakeSession:
    def __init__(self, status_code):
        self.status_code = status_code
        self.calls = []

    def _respond(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        return SimpleNamespace(
            status_code=self.status_code,
            ok=200 <= self.status_code < 300,
            text="boom",
        )

    def post(self, url, **kw):
        return self._respond("POST", url, **kw)

    def patch(self, url, **kw):
        return self._respond("PATCH", url, **kw)

    def delete(self, url, **kw):
        return self._respond("DELETE", url, **kw)


def _client(status_code):
    session = FakeSession(status_code)
    return GoogleCalendarClient("cal@group.calendar.google.com", session), session


def test_insert_reports_an_existing_event_instead_of_failing():
    client, _ = _client(409)
    assert client.insert({"id": "abc"}) is False


def test_insert_creates_with_a_timeout_and_an_escaped_calendar_id():
    client, session = _client(200)
    assert client.insert({"id": "abc"}) is True
    method, url, kwargs = session.calls[0]
    assert method == "POST"
    assert url.endswith("/calendars/cal%40group.calendar.google.com/events")
    assert kwargs["timeout"] == 5


@pytest.mark.parametrize("status_code", [404, 410])
def test_deleting_a_missing_event_is_success(status_code):
    client, _ = _client(status_code)
    client.delete("abc")


def test_other_errors_raise_with_the_status():
    client, _ = _client(500)
    with pytest.raises(CalendarError, match="500"):
        client.patch("abc", {"summary": "x"})
