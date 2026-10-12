"""follow ups

Revision ID: 5c2e8f1a9b3d
Revises: 07ee91fc2b95
Create Date: 2026-10-12 12:00:00.000000

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

from app.core.config import settings

# revision identifiers, used by Alembic.
revision: str = "5c2e8f1a9b3d"
down_revision: Union[str, None] = "07ee91fc2b95"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "follow_ups",
        sa.Column(
            "id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), nullable=False
        ),
        sa.Column("customer_id", sa.Uuid(), nullable=False),
        sa.Column("product_id", sa.Uuid(), nullable=False),
        sa.Column("due_date", sa.Date(), nullable=False),
        sa.Column("status", sa.String(length=10), nullable=False),
        sa.Column("close_reason", sa.String(length=10), nullable=True),
        sa.Column("sale_id", sa.Uuid(), nullable=True),
        sa.Column("closed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("closed_by", sa.Uuid(), nullable=True),
        sa.Column("sync_status", sa.String(length=10), nullable=False),
        sa.Column("sync_error", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "status IN ('open', 'closed')", name=op.f("ck_follow_ups_status")
        ),
        sa.CheckConstraint(
            "close_reason IS NULL OR close_reason IN ('sale', 'postponed', 'dismissed')",
            name=op.f("ck_follow_ups_close_reason"),
        ),
        sa.CheckConstraint(
            "sync_status IN ('pending', 'synced', 'failed', 'skipped')",
            name=op.f("ck_follow_ups_sync_status"),
        ),
        sa.CheckConstraint(
            "(status = 'open') = (close_reason IS NULL AND closed_at IS NULL)",
            name=op.f("ck_follow_ups_closed_fields"),
        ),
        sa.ForeignKeyConstraint(
            ["customer_id"],
            ["customers.id"],
            name=op.f("fk_follow_ups_customer_id_customers"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["product_id"],
            ["products.id"],
            name=op.f("fk_follow_ups_product_id_products"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["sale_id"],
            ["sales.id"],
            name=op.f("fk_follow_ups_sale_id_sales"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["closed_by"], ["users.id"], name=op.f("fk_follow_ups_closed_by_users")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_follow_ups")),
    )
    op.create_index(
        op.f("ix_follow_ups_customer_id"), "follow_ups", ["customer_id"], unique=False
    )
    op.create_index(
        op.f("ix_follow_ups_product_id"), "follow_ups", ["product_id"], unique=False
    )
    op.create_index(
        "uq_follow_ups_open_pair",
        "follow_ups",
        ["customer_id", "product_id"],
        unique=True,
        postgresql_where=sa.text("status = 'open'"),
    )

    # Carga inicial: un seguimiento abierto por ciclo con proyeccion. Los
    # vencidos al migrar quedan `skipped`: la app los muestra vencidos, pero
    # Google no se llena de eventos en fechas pasadas.
    op.execute(
        sa.text(
            """
            INSERT INTO follow_ups (customer_id, product_id, due_date, status, sync_status)
            SELECT customer_id, product_id, estimated_next_purchase, 'open',
                   CASE
                       WHEN estimated_next_purchase < (now() AT TIME ZONE :tz)::date
                       THEN 'skipped'
                       ELSE 'pending'
                   END
            FROM customer_product_cycles
            WHERE estimated_next_purchase IS NOT NULL
            """
        ).bindparams(tz=settings.BUSINESS_TIMEZONE)
    )

    # Nunca se escribio: el id del evento ahora es el id del seguimiento.
    op.drop_column("customer_product_cycles", "calendar_event_id")


def downgrade() -> None:
    op.add_column(
        "customer_product_cycles",
        sa.Column("calendar_event_id", sa.String(length=255), nullable=True),
    )
    op.drop_index("uq_follow_ups_open_pair", table_name="follow_ups")
    op.drop_index(op.f("ix_follow_ups_product_id"), table_name="follow_ups")
    op.drop_index(op.f("ix_follow_ups_customer_id"), table_name="follow_ups")
    op.drop_table("follow_ups")
