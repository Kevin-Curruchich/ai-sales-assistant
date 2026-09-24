"""cash movement type varchar check

Revision ID: 7c056dc387a2
Revises: 559bd2d61ba6
Create Date: 2026-09-24 14:59:33.721423

Ruling del coordinador sobre `559bd2d61ba6`: esa revision agrego `saldo_inicial`
como un quinto valor del enum NATIVO `cash_movement_type_enum` porque eso era lo
que realmente estaba desplegado (verificado en vivo, ver la nota de esa
revision). Pero el spec dice, verbal, "el patron de PaymentMethod" -- y ese
patron es VARCHAR + CHECK, no enum nativo (`app/models/payment_method.py`,
`PAYMENT_METHOD_COLUMN`). El enum nativo era un defecto del plan que describia
mal el estado del schema, no una intencion de usar un enum nativo aqui.

Ademas de la ergonomia con Alembic (`ALTER TYPE ... ADD VALUE` peleando con el
DDL transaccional -- lo que `559bd2d61ba6` tuvo que resolver a mano), hay un
motivo de correccion: los enums nativos de Postgres son schema-scoped.
`db_v2.cash_movement_type_enum` y `db_local.cash_movement_type_enum` son tipos
distintos aunque compartan nombre, y ese patron de tipos por-schema ya causo
problemas de comparacion de tipos entre `db_dev` y `db_v2` en el cutover (ver
docs/superpowers/handoffs/2026-09-23-piezas-2-3-agente.md, nota operativa sobre
`copy_schema_data.py`).

Y el momento importa: `cash_movements` tiene cero filas hoy (verificado contra
db_local, que espeja produccion). La Task 8 de este mismo plan es la que
empieza a escribir filas ahi. La conversion es gratis ahora; deja de serlo en
cuanto haya datos.

Por que dos revisiones y no una que reescriba `559bd2d61ba6`: esa revision ya
estaba commiteada como el registro honesto de lo que estaba desplegado en el
momento en que se escribio (un enum nativo de 4 valores, agregar el quinto).
Reescribirla borraria esa evidencia. Esta revision, separada, documenta la
correccion como lo que fue: una decision posterior, tomada al revisar contra
el spec, no parte del analisis original.

El `upgrade` convierte la columna a VARCHAR(20) (mismo largo que declara
CASH_MOVEMENT_TYPE en app/models/cash_movement.py), crea el CHECK con los
cinco valores seguiendo la misma convocatoria de nombre que uso
9bd1cde470ea para `payment_method` (`ck_{table}_{constraint_name}` como
argumento de `op.create_check_constraint`, que la naming convention de
Base.metadata en app/core/database.py vuelve a envolver con el prefijo
`ck_%(table_name)s_`), y borra el tipo enum que ya no se usa. El `downgrade`
hace exactamente lo inverso, con el `USING` explicito en ambos sentidos aunque
la tabla este vacia: escrito como si tuviera filas, porque en cuanto Task 8
corra dejara de estarlo.
"""
from typing import Sequence, Union

from alembic import op


# revision identifiers, used by Alembic.
revision: str = '7c056dc387a2'
down_revision: Union[str, None] = '559bd2d61ba6'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_CHECK_NAME = "ck_cash_movements_type"
_VALUES = ("entrada", "salida", "aporte_socio", "retiro_socio", "saldo_inicial")


def upgrade() -> None:
    op.execute("ALTER TABLE cash_movements ALTER COLUMN type TYPE VARCHAR(20) USING type::text")
    values = ", ".join(f"'{v}'" for v in _VALUES)
    op.create_check_constraint(_CHECK_NAME, "cash_movements", f"type IN ({values})")
    op.execute("DROP TYPE cash_movement_type_enum")


def downgrade() -> None:
    values = ", ".join(f"'{v}'" for v in _VALUES)
    op.execute(f"CREATE TYPE cash_movement_type_enum AS ENUM ({values})")
    op.drop_constraint(_CHECK_NAME, "cash_movements", type_="check")
    op.execute(
        "ALTER TABLE cash_movements ALTER COLUMN type TYPE cash_movement_type_enum "
        "USING type::text::cash_movement_type_enum"
    )
