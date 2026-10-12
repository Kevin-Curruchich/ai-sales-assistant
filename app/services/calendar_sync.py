"""Espejo de los seguimientos en Google Calendar.

Quien escribe seguimientos llama a `request_calendar_sync()` despues de su
commit; eso solo enciende una senal. El hilo que la atiende (mas abajo, Task 7)
duerme sin temporizador mientras no haya trabajo.
"""

import threading

_wake = threading.Event()


def request_calendar_sync() -> None:
    """Pide una pasada de sincronizacion. No bloquea ni falla."""
    _wake.set()
