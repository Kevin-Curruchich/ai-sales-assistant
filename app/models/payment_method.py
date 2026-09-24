from enum import Enum as PyEnum

from sqlalchemy import Enum as SQLEnum


class PaymentMethod(str, PyEnum):
    """Medios de pago aceptados.

    Se renderiza como VARCHAR + CHECK, no como enum nativo de Postgres: este
    vocabulario crece (manana tarjeta, deposito), y ALTER TYPE ADD VALUE pelea
    con el DDL transaccional de Alembic, mientras que quitar un valor de un enum
    nativo es practicamente imposible.  Con un CHECK, cambiar la lista es soltar
    y recrear la constraint.
    """

    EFECTIVO = "efectivo"
    TRANSFERENCIA = "transferencia"


# length=50 a proposito: un medio de pago con nombre largo no debe obligar a
# redimensionar la columna ademas de tocar el CHECK.
PAYMENT_METHOD_COLUMN = SQLEnum(
    PaymentMethod,
    name="payment_method",
    native_enum=False,
    length=50,
    validate_strings=True,
    values_callable=lambda x: [e.value for e in x],
)
