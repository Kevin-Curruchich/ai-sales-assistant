from sqlalchemy import Numeric

from app.core.database import Base
import app.models  # noqa: F401

DECIMAL_QUANTITY_COLUMNS = [
    ("products", "stock"),
    ("products", "min_stock"),
    ("purchase_items", "quantity"),
    ("purchase_items", "remaining_quantity"),
    ("sale_items", "quantity"),
    ("sale_item_lot_allocations", "quantity_allocated"),
    ("customer_product_cycles", "last_quantity"),
]


def test_quantity_columns_are_all_numeric_10_4():
    """Vender medio carton debe funcionar en cualquier columna de cantidad."""
    for table, column in DECIMAL_QUANTITY_COLUMNS:
        col = Base.metadata.tables[table].c[column]
        assert isinstance(col.type, Numeric), f"{table}.{column} no es Numeric"
        assert (col.type.precision, col.type.scale) == (10, 4), (
            f"{table}.{column} es Numeric({col.type.precision},{col.type.scale})"
        )
