"""cash movement saldo inicial

Revision ID: 559bd2d61ba6
Revises: a21d7bd9293c
Create Date: 2026-09-24 14:53:26.160097

`saldo_inicial` es el quinto valor de `CashMovementType`: el saldo en efectivo
al momento del corte, que no es ni una venta ni una deuda con el socio (no
entra en CASH_OUTFLOW_TYPES, suma igual que `entrada`).

Correccion manual sobre el plan: el plan (y el brief de esta task) asumian que
`cash_movements.type` era un VARCHAR con CHECK, igual que `payment_method`
(revision 9bd1cde470ea). No lo es. `type` sigue siendo el enum NATIVO de
Postgres que creo 8df5b1f79497
(`sa.Enum(..., name='cash_movement_type_enum')`, y `native_enum=True` en
app/models/cash_movement.py); nunca hubo una migracion que lo convirtiera a
VARCHAR+CHECK como si le paso a `payment_method`. Verificado en vivo contra
revenew-dev-db:

  SELECT conname, contype FROM pg_constraint
  WHERE conrelid = 'cash_movements'::regclass;
  -- ck_cash_movements_ck_cash_movements_payment_method | c   (payment_method)
  -- pk_cash_movements                                  | p
  -- fk_cash_movements_purchase_id_purchases            | f
  -- fk_cash_movements_sale_id_sales                    | f
  -- (ningun CHECK sobre "type")

  SELECT udt_name FROM information_schema.columns
  WHERE table_name = 'cash_movements' AND column_name = 'type';
  -- cash_movement_type_enum (USER-DEFINED, es decir, un enum nativo)

Por eso esta migracion no hace drop/create de un CHECK: agrega el valor al
tipo enum con ALTER TYPE ... ADD VALUE. Es justo el dolor que la nota de
9bd1cde470ea describe para justificar VARCHAR+CHECK en payment_method
("ALTER TYPE ... ADD VALUE no puede correr dentro de una transaccion" en
versiones viejas de Postgres) pero Postgres 17 (la version real de
revenew-dev-db y del test-db desechable) permite ADD VALUE dentro de un
bloque de transaccion desde la 12; la unica restriccion que sigue vigente es
que el valor nuevo no se puede *usar* en la misma transaccion en que se
agrega, y esta revision no lo usa, solo lo agrega.

El downgrade no puede hacer DROP VALUE (Postgres no lo soporta para enums
nativos), asi que reconstruye el tipo: borra primero las filas saldo_inicial
(violarian el enum viejo), renombra el tipo actual, crea uno nuevo con el
vocabulario de cuatro valores, reapunta la columna con USING, y borra el tipo
viejo. Ninguna operacion cualifica el schema destino: se resuelve por el
search_path que fija env.py, igual que el resto de las revisiones.
"""

from typing import Sequence, Union

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "559bd2d61ba6"
down_revision: Union[str, None] = "a21d7bd9293c"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("ALTER TYPE cash_movement_type_enum ADD VALUE 'saldo_inicial'")


def downgrade() -> None:
    # Las filas saldo_inicial violarian el enum viejo (sin ese valor): se
    # borran primero, igual que haria un DELETE contra un CHECK mas estricto.
    op.execute("DELETE FROM cash_movements WHERE type = 'saldo_inicial'")
    # Postgres no soporta "ALTER TYPE ... DROP VALUE": hay que recrear el tipo
    # sin el valor y reapuntar la columna.
    op.execute(
        "ALTER TYPE cash_movement_type_enum RENAME TO cash_movement_type_enum_old"
    )
    op.execute(
        "CREATE TYPE cash_movement_type_enum AS ENUM "
        "('entrada', 'salida', 'aporte_socio', 'retiro_socio')"
    )
    op.execute(
        "ALTER TABLE cash_movements ALTER COLUMN type TYPE cash_movement_type_enum "
        "USING type::text::cash_movement_type_enum"
    )
    op.execute("DROP TYPE cash_movement_type_enum_old")
