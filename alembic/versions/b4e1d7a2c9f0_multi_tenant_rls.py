"""multi tenant rls

Revision ID: b4e1d7a2c9f0
Revises: 5c2e8f1a9b3d
Create Date: 2026-10-26 12:00:00.000000

"""

import uuid
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

from app.core.config import settings

# revision identifiers, used by Alembic.
revision: str = "b4e1d7a2c9f0"
down_revision: Union[str, None] = "5c2e8f1a9b3d"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# Copiado y no importado del modelo: una migracion no debe cambiar si el
# modelo cambia despues.
CURRENT_BUSINESS_ID = "nullif(current_setting('app.business_id', true), '')::uuid"

RLS_TABLES = (
    "customers",
    "products",
    "purchases",
    "purchase_items",
    "sales",
    "sale_items",
    "sale_item_lot_allocations",
    "cash_movements",
    "follow_ups",
    "customer_product_cycles",
    "agent_threads",
)
# users lleva business_id pero no RLS: se consulta antes de conocer el comercio.
BUSINESS_ID_TABLES = ("users",) + RLS_TABLES

# (tabla, columna, nombre del unique por comercio)
PER_BUSINESS_UNIQUES = (
    ("products", "sku", "uq_products_business_sku"),
    ("purchases", "reference_number", "uq_purchases_business_reference_number"),
)


def upgrade() -> None:
    op.create_table(
        "businesses",
        sa.Column(
            "id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), nullable=False
        ),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column(
            "timezone",
            sa.String(length=64),
            server_default=sa.text("'America/Guatemala'"),
            nullable=False,
        ),
        sa.Column("google_calendar_id", sa.String(length=255), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_businesses")),
    )

    # El comercio actual: todos los datos existentes pasan a ser suyos.
    business_id = uuid.uuid4()
    op.execute(
        sa.text(
            "INSERT INTO businesses (id, name, timezone, google_calendar_id) "
            "VALUES (:id, 'Comercio principal', :tz, :calendar)"
        ).bindparams(
            id=business_id,
            tz=settings.BUSINESS_TIMEZONE,
            calendar=settings.GOOGLE_CALENDAR_ID,
        )
    )

    for table in BUSINESS_ID_TABLES:
        op.add_column(table, sa.Column("business_id", sa.Uuid(), nullable=True))
        op.execute(
            sa.text(f"UPDATE {table} SET business_id = :id").bindparams(id=business_id)
        )
        op.alter_column(
            table,
            "business_id",
            nullable=False,
            server_default=sa.text(CURRENT_BUSINESS_ID),
        )
        op.create_foreign_key(
            op.f(f"fk_{table}_business_id_businesses"),
            table,
            "businesses",
            ["business_id"],
            ["id"],
        )

    for table, column, uq_name in PER_BUSINESS_UNIQUES:
        op.drop_index(op.f(f"ix_{table}_{column}"), table_name=table)
        op.create_index(op.f(f"ix_{table}_{column}"), table, [column], unique=False)
        op.create_unique_constraint(uq_name, table, ["business_id", column])

    # El rol es de todo el cluster y lo comparten los schemas (db_dev, db_v2,
    # los de test): se crea solo si falta y el downgrade no lo borra.  Los
    # GRANTs si son del schema que se esta migrando.
    op.execute(
        """
        DO $$
        BEGIN
            IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'app_tenant') THEN
                BEGIN
                    CREATE ROLE app_tenant NOLOGIN NOSUPERUSER NOBYPASSRLS;
                EXCEPTION WHEN duplicate_object THEN
                    NULL;  -- otra migracion concurrente lo creo primero
                END;
            END IF;
            EXECUTE format('GRANT USAGE ON SCHEMA %I TO app_tenant', current_schema());
            EXECUTE format(
                'GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA %I TO app_tenant',
                current_schema());
            EXECUTE format(
                'GRANT USAGE ON ALL SEQUENCES IN SCHEMA %I TO app_tenant', current_schema());
            EXECUTE format(
                'ALTER DEFAULT PRIVILEGES IN SCHEMA %I '
                'GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO app_tenant',
                current_schema());
            EXECUTE format(
                'ALTER DEFAULT PRIVILEGES IN SCHEMA %I GRANT USAGE ON SEQUENCES TO app_tenant',
                current_schema());
        END
        $$
        """
    )

    # Sin variable fijada la expresion es NULL y la comparacion nunca es
    # verdadera: 0 filas al leer y error al escribir (falla cerrado).
    for table in RLS_TABLES:
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
        op.execute(
            f"CREATE POLICY tenant_isolation ON {table} "
            f"USING (business_id = {CURRENT_BUSINESS_ID}) "
            f"WITH CHECK (business_id = {CURRENT_BUSINESS_ID})"
        )


def downgrade() -> None:
    for table in RLS_TABLES:
        op.execute(f"DROP POLICY tenant_isolation ON {table}")
        op.execute(f"ALTER TABLE {table} NO FORCE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} DISABLE ROW LEVEL SECURITY")

    # Se revocan los GRANTs de este schema; el rol queda (lo usan otros).
    op.execute(
        """
        DO $$
        BEGIN
            EXECUTE format(
                'ALTER DEFAULT PRIVILEGES IN SCHEMA %I REVOKE USAGE ON SEQUENCES FROM app_tenant',
                current_schema());
            EXECUTE format(
                'ALTER DEFAULT PRIVILEGES IN SCHEMA %I '
                'REVOKE SELECT, INSERT, UPDATE, DELETE ON TABLES FROM app_tenant',
                current_schema());
            EXECUTE format(
                'REVOKE ALL ON ALL SEQUENCES IN SCHEMA %I FROM app_tenant', current_schema());
            EXECUTE format(
                'REVOKE ALL ON ALL TABLES IN SCHEMA %I FROM app_tenant', current_schema());
            EXECUTE format('REVOKE USAGE ON SCHEMA %I FROM app_tenant', current_schema());
        END
        $$
        """
    )

    # Falla si dos comercios repiten un sku o una referencia: volver al unique
    # global sin perder datos no es posible en ese caso.
    for table, column, uq_name in PER_BUSINESS_UNIQUES:
        op.drop_constraint(uq_name, table, type_="unique")
        op.drop_index(op.f(f"ix_{table}_{column}"), table_name=table)
        op.create_index(op.f(f"ix_{table}_{column}"), table, [column], unique=True)

    for table in reversed(BUSINESS_ID_TABLES):
        op.drop_constraint(
            op.f(f"fk_{table}_business_id_businesses"), table, type_="foreignkey"
        )
        op.drop_column(table, "business_id")

    op.drop_table("businesses")
