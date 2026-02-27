# Revenew — Data Models Documentation

This document describes each database model, its responsibility, fields, and how it relates to other models in the system.

---

## Entity Relationship Diagram

```
┌──────────┐       ┌──────────┐       ┌──────────────┐
│   User   │──1:N──│          │──1:N──│     Sale      │
│ (auth)   │       │ Customer │       │              │
└──────────┘       │          │       │  ┌─────────┐ │
                   │          │       └──│SaleItem  │─┘
                   │          │          │          │
                   │          │          └────┬─────┘
                   │          │               │
                   │          │──1:N──┐       │ N:1
                   └──────────┘       │  ┌────┴─────┐
                                      └──│ Customer  │
                                         │ Product   │
                                         │ Cycle     │
                                         └────┬─────┘
                                              │ N:1
                                         ┌────┴─────┐
                                         │ Product   │
                                         └──────────┘
```

---

## 1. User

**Table:** `users`  
**File:** `app/models/user.py`

### Responsibility

Represents a system user who accesses the Revenew admin panel. Authentication is handled by **Firebase Auth**; this model stores a local mirror of the user for **role-based access control** (RBAC).

The `id` (UUID) is **shared between Firebase and the local database**. During signup, a UUID is generated locally and passed to Firebase as the `uid`. For users who authenticate before signing up via the API, the Firebase UID is used as the local `id`.

### Fields

| Column | Type | Description |
|---|---|---|
| `id` | `uuid` (PK) | UUID primary key — **same value as the Firebase UID**. Generated locally on signup and sent to Firebase |
| `email` | `str(255)` | User's email (synced from Firebase) |
| `display_name` | `str(255)?` | Optional display name |
| `role` | `str(50)` | One of `admin`, `sales_rep`, `viewer`. Default: `viewer` |
| `is_active` | `bool` | Whether the user can access the system. Default: `True` |
| `created_at` | `datetime` | Auto-set on creation |
| `updated_at` | `datetime` | Auto-set on update |

### Relationships

| Relation | Target | Type | Cascade |
|---|---|---|---|
| `sales` | `Sale` | 1:N | — |

---

## 2. Customer

**Table:** `customers`  
**File:** `app/models/customer.py`

### Responsibility

Represents a client/buyer of the business. Stores contact information and serves as the parent entity for sales and replenishment cycle tracking.

### Fields

| Column | Type | Description |
|---|---|---|
| `id` | `uuid` (PK) | UUID primary key |
| `name` | `str(255)` | Customer name (required) |
| `company` | `str(255)?` | Company or business name |
| `email` | `str(255)?` | Contact email |
| `phone` | `str(50)?` | Contact phone |
| `address` | `text?` | Physical address |
| `notes` | `text?` | Free-form notes |
| `created_at` | `datetime` | Auto-set on creation |
| `updated_at` | `datetime` | Auto-set on update |

### Relationships

| Relation | Target | Type | Cascade |
|---|---|---|---|
| `sales` | `Sale` | 1:N | `all, delete-orphan` |
| `product_cycles` | `CustomerProductCycle` | 1:N | `all, delete-orphan` |

---

## 3. Product

**Table:** `products`  
**File:** `app/models/product.py`

### Responsibility

Represents an item in the business catalog/inventory. Tracks current stock level and a **minimum stock threshold** (`min_stock`) used to trigger reorder alerts when upcoming customer demand could deplete inventory.

### Fields

| Column | Type | Description |
|---|---|---|
| `id` | `uuid` (PK) | UUID primary key |
| `sku` | `str(100)` | Stock-Keeping Unit — unique, indexed |
| `name` | `str(255)` | Product name (required) |
| `description` | `text?` | Optional description |
| `price` | `float` | Current catalog price |
| `stock` | `int` | Current units in inventory. Deducted automatically when a sale is created |
| `min_stock` | `int` | **Reorder point.** When `stock <= min_stock`, follow-up views flag a stock alert |
| `status` | `str(20)` | `active` or `inactive`. Default: `active` |
| `created_at` | `datetime` | Auto-set on creation |
| `updated_at` | `datetime` | Auto-set on update |

### Relationships

| Relation | Target | Type | Cascade |
|---|---|---|---|
| `customer_cycles` | `CustomerProductCycle` | 1:N | `all, delete-orphan` |

---

## 4. Sale

**Table:** `sales`  
**File:** `app/models/sale.py`

### Responsibility

Represents a single sales transaction for a customer. Acts as a header record grouping multiple line items (`SaleItem`). When a sale is created, the system automatically:

1. Deducts `stock` from each product.
2. Recalculates the `CustomerProductCycle` for every customer+product pair in the sale.

### Fields

| Column | Type | Description |
|---|---|---|
| `id` | `uuid` (PK) | UUID primary key |
| `customer_id` | `uuid` (FK) | References `customers.id` — indexed |
| `user_id` | `uuid` (FK) | References `users.id` — indexed. The user (sales rep) who created the sale |
| `date` | `date` | Date of the sale |
| `total` | `float` | Sum of all item subtotals (computed on creation) |
| `created_at` | `datetime` | Auto-set on creation |
| `updated_at` | `datetime` | Auto-set on update |

### Relationships

| Relation | Target | Type | Cascade |
|---|---|---|---|
| `customer` | `Customer` | N:1 | — |
| `user` | `User` | N:1 | — |
| `items` | `SaleItem` | 1:N | `all, delete-orphan` |

---

## 5. SaleItem

**Table:** `sale_items`  
**File:** `app/models/sale_item.py`

### Responsibility

Represents a single line item within a sale — which product was sold, how many units, at what price. Stores the **historical unit price** at the time of purchase (which may differ from the product's current catalog price).

### Fields

| Column | Type | Description |
|---|---|---|
| `id` | `uuid` (PK) | UUID primary key |
| `sale_id` | `uuid` (FK) | References `sales.id` — indexed |
| `product_id` | `uuid` (FK) | References `products.id` — indexed |
| `quantity` | `int` | Number of units sold |
| `unit_price` | `float` | Price per unit **at time of sale** |
| `subtotal` | `float` | `quantity × unit_price` |
| `created_at` | `datetime` | Auto-set on creation |

### Relationships

| Relation | Target | Type |
|---|---|---|
| `sale` | `Sale` | N:1 |
| `product` | `Product` | N:1 |

---

## 6. CustomerProductCycle

**Table:** `customer_product_cycles`  
**File:** `app/models/customer_product_cycle.py`

### Responsibility

**Core prediction model.** Tracks the replenishment pattern for each **customer + product** pair. Every time a sale is created, the system recalculates this record by:

1. Collecting all historical purchase dates for this customer+product pair.
2. Computing the **average interval** (in days) between consecutive purchases.
3. Projecting the **estimated next purchase date** from the most recent purchase.

This data powers three key views:

- **Follow-ups:** "Customer A will need Product X in 5 days, and I only have 2 units left."
- **Calendar:** Visual timeline of predicted replenishment dates per customer and product.
- **Dashboard:** Aggregated metrics (overdue, next 7/14/30 days).

### Fields

| Column | Type | Description |
|---|---|---|
| `id` | `uuid` (PK) | UUID primary key |
| `customer_id` | `uuid` (FK) | References `customers.id` — indexed |
| `product_id` | `uuid` (FK) | References `products.id` — indexed |
| `avg_interval_days` | `int?` | Average days between purchases. `30` (default) on first purchase, recalculated on subsequent ones |
| `estimated_next_purchase` | `date?` | Projected date the customer will need this product again |
| `last_purchase_date` | `date` | Date of the most recent purchase of this product by this customer |
| `last_quantity` | `int` | Quantity ordered in the most recent purchase |
| `total_purchases` | `int` | Total number of times this customer has bought this product |
| `created_at` | `datetime` | Auto-set on creation |
| `updated_at` | `datetime` | Auto-set on update |

### Constraints

- **Unique:** `(customer_id, product_id)` — one cycle record per customer+product pair.

### Relationships

| Relation | Target | Type |
|---|---|---|
| `customer` | `Customer` | N:1 |
| `product` | `Product` | N:1 |
