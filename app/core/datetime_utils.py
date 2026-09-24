from datetime import date, datetime, timezone
from typing import Optional, Union
from zoneinfo import ZoneInfo

from app.core.config import settings

DATE_FORMAT = "%d/%m/%Y"
DATETIME_FORMAT = "%d/%m/%Y %H:%M"


def business_tz() -> ZoneInfo:
    return ZoneInfo(settings.BUSINESS_TIMEZONE)


def to_business_tz(value: datetime) -> datetime:
    """Proyecta un instante a la zona del negocio.

    Un datetime naive se asume UTC en vez de interpretarse en la zona del
    proceso: el servidor corre en UTC y el escritorio no, y esa diferencia
    silenciosa es de donde salen los desfases de seis horas.
    """
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(business_tz())


def format_business_date(value: Optional[Union[datetime, date]]) -> str:
    if value is None:
        return ""
    if isinstance(value, datetime):
        return to_business_tz(value).strftime(DATE_FORMAT)
    # Un date puro no tiene hora: no hay nada que convertir.
    return value.strftime(DATE_FORMAT)


def format_business_datetime(value: Optional[datetime]) -> str:
    if value is None:
        return ""
    return to_business_tz(value).strftime(DATETIME_FORMAT)
