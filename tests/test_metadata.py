from sqlalchemy.dialects import postgresql
from sqlalchemy.schema import CreateTable

from app.core.database import Base
import app.models  # noqa: F401


def test_metadata_carries_no_hardcoded_schema():
    assert Base.metadata.schema is None
    assert all(t.schema is None for t in Base.metadata.sorted_tables)


def test_enum_type_is_not_bound_to_a_schema():
    col = Base.metadata.tables["products"].c.earning_mode
    assert col.type.schema is None


def test_foreign_keys_follow_the_naming_convention():
    ddl = str(
        CreateTable(Base.metadata.tables["sales"]).compile(dialect=postgresql.dialect())
    )
    assert "fk_sales_customer_id_customers" in ddl
    assert "pk_sales" in ddl


def test_every_table_is_reachable_without_a_schema_prefix():
    expected = {
        "users", "customers", "products", "purchases", "purchase_items",
        "sales", "sale_items", "sale_item_lot_allocations", "customer_product_cycles",
    }
    assert expected <= set(Base.metadata.tables)
