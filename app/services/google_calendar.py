"""Cliente minimo de la API REST de Google Calendar v3.

Tres llamadas, con `AuthorizedSession` de google-auth: no hace falta
google-api-python-client. Los ids de evento los pone quien llama (el id del
seguimiento), lo que hace idempotente cada operacion.
"""

from urllib.parse import quote

import google.auth
from google.auth.transport.requests import AuthorizedSession
from google.oauth2 import service_account

from app.core.config import settings

CALENDAR_API = "https://www.googleapis.com/calendar/v3"
SCOPES = ["https://www.googleapis.com/auth/calendar.events"]
TIMEOUT_SECONDS = 5


class CalendarError(Exception):
    pass


class GoogleCalendarClient:
    def __init__(self, calendar_id: str, session):
        self.calendar_id = calendar_id
        self.session = session

    @classmethod
    def from_settings(cls) -> "GoogleCalendarClient":
        """La misma cuenta de servicio que usa Firebase."""
        if settings.FIREBASE_CREDENTIALS_PATH:
            creds = service_account.Credentials.from_service_account_file(
                settings.FIREBASE_CREDENTIALS_PATH, scopes=SCOPES
            )
        else:
            creds, _project = google.auth.default(scopes=SCOPES)
        return cls(settings.GOOGLE_CALENDAR_ID, AuthorizedSession(creds))

    def _url(self, event_id: str | None = None) -> str:
        base = f"{CALENDAR_API}/calendars/{quote(self.calendar_id, safe='')}/events"
        return f"{base}/{event_id}" if event_id else base

    @staticmethod
    def _raise_for(response) -> None:
        if not response.ok:
            raise CalendarError(f"{response.status_code}: {response.text[:500]}")

    def insert(self, event: dict) -> bool:
        """True si lo creo; False si ese id ya existia (409)."""
        response = self.session.post(self._url(), json=event, timeout=TIMEOUT_SECONDS)
        if response.status_code == 409:
            return False
        self._raise_for(response)
        return True

    def patch(self, event_id: str, fields: dict) -> None:
        response = self.session.patch(
            self._url(event_id), json=fields, timeout=TIMEOUT_SECONDS
        )
        self._raise_for(response)

    def delete(self, event_id: str) -> None:
        response = self.session.delete(self._url(event_id), timeout=TIMEOUT_SECONDS)
        if response.status_code in (404, 410):
            return
        self._raise_for(response)
