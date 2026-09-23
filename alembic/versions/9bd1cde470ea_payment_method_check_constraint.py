"""payment method check constraint

Revision ID: 9bd1cde470ea
Revises: 2208c8e60855
Create Date: 2026-09-23 16:11:39.864872

`payment_method` en `sales`, `purchases` y `cash_movements` ya era
VARCHAR(50) desde la revision 8df5b1f79497: esta revision no toca el tipo de
columna, solo agrega un CHECK que restringe los valores aceptados a
'efectivo' / 'transferencia' (o NULL).

Se eligio VARCHAR + CHECK en vez de un enum nativo de Postgres a proposito
(ver `app.models.payment_method.PaymentMethod`): el vocabulario va a crecer
(tarjeta, deposito, ...) y `ALTER TYPE ... ADD VALUE` no puede correr dentro
de una transaccion, mientras que sacar un valor de un enum nativo es
practicamente imposible.  Cambiar la lista de valores aceptados es soltar y
recrear el CHECK.

El CHECK acepta NULL a proposito: las 194 filas que hay hoy en produccion
(entre las tres tablas) tienen `payment_method` en NULL, y un CHECK de SQL
evalua a verdadero (no lo viola) cuando la columna es NULL, asi que esta
migracion no falla ni fuerza un backfill contra esos datos.

`--autogenerate` genero upgrade/downgrade vacios (ver mas abajo el porque) y
las seis operaciones (tres `op.create_check_constraint` / tres
`op.drop_constraint`) se escribieron a mano. Los nombres de las constraints
siguen la naming convention de `Base.metadata`
(`ck_%(table_name)s_%(constraint_name)s`, en app/core/database.py), tomando
"payment_method" como `constraint_name` porque es el `name=` que lleva
`PAYMENT_METHOD_COLUMN` en app/models/payment_method.py.

Por que salio vacio el autogenerate: `sqlalchemy.Enum(..., native_enum=False)`
tiene `create_constraint=False` por default desde SQLAlchemy 1.4/2.0, asi que
el metadata de los modelos no declara ningun `CheckConstraint` para
comparar; autogenerate no tiene nada del lado "target" con qué comparar el
CHECK que falta del lado de la base, y no emite nada. De ahi la advertencia
del plan: cuando el CHECK no aparece autogenerado, hay que escribirlo a mano.

Como en las revisiones anteriores, ninguna operacion cualifica el schema
destino (se resuelve por el search_path que fija env.py), y no aparece
`CREATE TYPE` ni `ALTER TYPE` en ningun lado: si aparecieran, seria señal de
que `native_enum=False` no tomo efecto.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '9bd1cde470ea'
down_revision: Union[str, None] = '2208c8e60855'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_PAYMENT_METHOD_VALUES = ("efectivo", "transferencia")
_TABLES = ("sales", "purchases", "cash_movements")


def upgrade() -> None:
    values = ", ".join(f"'{v}'" for v in _PAYMENT_METHOD_VALUES)
    for table in _TABLES:
        op.create_check_constraint(
            f"ck_{table}_payment_method",
            table,
            f"payment_method IN ({values})",
        )


def downgrade() -> None:
    for table in _TABLES:
        op.drop_constraint(f"ck_{table}_payment_method", table, type_="check")
