from datetime import date, datetime, time, timezone
from typing import Optional, Union
from zoneinfo import ZoneInfo

from sqlalchemy.orm import Session

DATE_FORMAT = "%d/%m/%Y"
DATETIME_FORMAT = "%d/%m/%Y %H:%M"


class MissingBusinessContext(RuntimeError):
    """La sesion no esta ligada a un comercio (`bind_business`)."""


def business_tz(db: Session) -> ZoneInfo:
    # Sin fallback a una zona global: adivinar la zona de otro comercio es peor que fallar.
    tz = db.info.get("business_tz")
    if not tz:
        raise MissingBusinessContext("La sesion no tiene business_tz")
    return ZoneInfo(tz)


def business_today(db: Session) -> date:
    """El dia de hoy en la zona del negocio, no en la del servidor.

    Railway corre en UTC y Guatemala esta seis horas atras: entre las 18:00 y
    la medianoche local, `date.today()` ya devuelve el dia siguiente.
    """
    return datetime.now(business_tz(db)).date()


def business_midnight(db: Session, day: date) -> datetime:
    """El instante en que empieza `day` en la zona del negocio."""
    return datetime.combine(day, time.min, tzinfo=business_tz(db))


def business_instant(db: Session, day: date) -> datetime:
    """El instante de un hecho del que solo se conoce el dia.

    Si `day` es hoy en la zona del negocio, el hecho esta ocurriendo ahora:
    se guarda la hora real, que es la que permite ordenar y leer los
    movimientos de un mismo dia.  Un dia pasado no tiene hora conocida e
    inventarla seria peor: se ancla a `business_midnight()`.
    """
    now = datetime.now(business_tz(db))
    if day == now.date():
        return now
    return business_midnight(db, day)


def business_end_of_day(db: Session, day: date) -> datetime:
    """El ultimo instante de `day` en la zona del negocio.

    Contraparte de `business_midnight()`: mismo patron (combine + tzinfo de la
    zona del negocio), fin del dia en vez de inicio. Necesaria para un limite
    SUPERIOR inclusivo: `occurred_at <= business_end_of_day(day)` cubre el dia
    local completo, mientras que combinar con medianoche naive (o asumir UTC)
    corta el dia entre 6 y 7 horas antes de que termine en hora de Guatemala.
    """
    return datetime.combine(day, time.max, tzinfo=business_tz(db))


def to_business_tz(db: Session, value: datetime) -> datetime:
    """Proyecta un instante a la zona del negocio.

    Un datetime naive se asume UTC en vez de interpretarse en la zona del
    proceso: el servidor corre en UTC y el escritorio no, y esa diferencia
    silenciosa es de donde salen los desfases de seis horas.
    """
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(business_tz(db))


def format_business_date(db: Session, value: Optional[Union[datetime, date]]) -> str:
    if value is None:
        return ""
    if isinstance(value, datetime):
        return to_business_tz(db, value).strftime(DATE_FORMAT)
    # Un date puro no tiene hora: no hay nada que convertir.
    return value.strftime(DATE_FORMAT)


def format_business_datetime(db: Session, value: Optional[datetime]) -> str:
    if value is None:
        return ""
    return to_business_tz(db, value).strftime(DATETIME_FORMAT)
