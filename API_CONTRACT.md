# Revenew API Contract

> **Base URL:** `/api/v1`  
> **Auth:** All endpoints (except `POST /auth/signup`) require a Firebase ID token in the `Authorization: Bearer <token>` header. The backend verifies the token, auto-creates a local user on first call, and enforces role-based access where noted.

> **Note:** All `id`, `{user_id}`, `{customer_id}`, `{product_id}`, and `{sale_id}` values are **UUID v4** strings (e.g. `"a3f1b2c4-5d6e-7f8a-9b0c-1d2e3f4a5b6c"`). All path parameters and foreign key references use UUIDs.

---

## 1. Auth Module

### `POST /auth/signup`

Register a new user. Creates a Firebase Authentication account with custom claims and a local DB record. **No auth required.** A UUID is generated locally and used as the `uid` in Firebase, ensuring a single shared identifier.

- **Request Body:**
  ```json
  {
    "email": "string",
    "password": "string",
    "display_name": "string | null",
    "role": "admin | sales_rep | viewer"
  }
  ```
- **Response (201):** `User`

### `GET /auth/me`

Returns the authenticated user's profile (auto-created on first call).

- **Response:** `User`

### `GET /auth/users`  — Admin only

List all users.

- **Response:** `User[]`

### `GET /auth/users/{user_id}`  — Admin only

Get a user by ID.

- **Response:** `User`

### `PUT /auth/users/{user_id}`  — Admin only

Update a user's profile.

- **Request Body:**
  ```json
  {
    "display_name": "string | null",
    "is_active": "boolean | null",
    "role": "string | null"
  }
  ```
- **Response:** `User`

### `PATCH /auth/users/{user_id}/role`  — Admin only

Change a user's role.

- **Request Body:**
  ```json
  {
    "role": "admin | sales_rep | viewer"
  }
  ```
- **Response:** `User`

---

## 2. Dashboard Module

### `GET /dashboard/summary`

Aggregated metrics for the main dashboard.

- **Response:**
  ```json
  {
    "totalCustomers": 0,
    "salesThisMonth": 0.0,
    "pendingFollowUps": 0,
    "upcomingPurchases7Days": 0,
    "recentSales": [ /* Sale[] */ ],
    "priorityCustomers": [ /* FollowUp[] */ ]
  }
  ```

---

## 3. Products Module

### `GET /products`

- **Query Params:** `search` (string, optional), `status_filter` (`active` | `inactive`, optional)
- **Response:** `Product[]`

### `GET /products/{product_id}`

- **Response:** `Product`

### `POST /products`

- **Request Body:**
  ```json
  {
    "sku": "string",
    "name": "string",
    "description": "string | null",
    "price": 0.0,
    "stock": 0,
    "min_stock": 0,
    "status": "active | inactive"
  }
  ```
- **Response (201):** `Product`

### `PUT /products/{product_id}`

- **Request Body:** Same as POST (all fields optional)
- **Response:** `Product`

### `DELETE /products/{product_id}`

- **Response:** `204 No Content`

---

## 4. Customers Module

### `GET /customers`

- **Query Params:** `search` (string, optional)
- **Response:** `Customer[]`

### `GET /customers/{customer_id}`

- **Response:** `Customer`

### `POST /customers`

- **Request Body:**
  ```json
  {
    "name": "string",
    "company": "string | null",
    "email": "string | null",
    "phone": "string | null",
    "address": "string | null",
    "notes": "string | null"
  }
  ```
- **Response (201):** `Customer`

### `PUT /customers/{customer_id}`

- **Request Body:** Same as POST (all fields optional)
- **Response:** `Customer`

---

## 5. Sales Module

When a sale is created the backend automatically:
1. Deducts `stock` from each product.
2. Recalculates the `CustomerProductCycle` for every customer+product pair.
3. Associates the sale with the authenticated user (`user_id`).

### `GET /sales`

- **Query Params:** `customer_id` (uuid, optional), `start_date` (YYYY-MM-DD, optional), `end_date` (YYYY-MM-DD, optional)
- **Response:** `Sale[]`

### `GET /sales/{sale_id}`

- **Response:** `Sale`

### `POST /sales`

- **Request Body:**
  ```json
  {
    "customerId": "uuid",
    "date": "YYYY-MM-DD",
    "isPaymentPending": false,
    "items": [
      { "productId": "uuid", "quantity": 2, "unitPrice": 15.5 }
    ]
  }
  ```
- **Response (201):** `Sale` (includes computed `total`, item `subtotal`s, and the `user_id` of the creator)

### `PUT /sales/{sale_id}`

- **Request Body:**
  ```json
  {
    "customerId": "uuid | null",
    "date": "date | null",
    "items": "SaleItemCreate[] | null",
    "isPaymentPending": "boolean | null"
  }
  ```
- **Response:** `Sale`

### `PATCH /sales/{sale_id}/payment-status`

- **Request Body:**
  ```json
  {
    "isPaymentPending": true
  }
  ```
- **Response:** `Sale`

---

## 6. Follow-up Module

Follow-ups are computed from `CustomerProductCycle` records. Each follow-up groups all tracked products for a given customer.

### `GET /follow-ups`

- **Query Params:** `filter` (enum: `all` | `overdue` | `7_days` | `14_days` | `30_days`, default: `all`)
- **Response:** `FollowUp[]`

### `GET /follow-ups/metrics`

Counts for metric cards.

- **Response:**
  ```json
  {
    "overdue": 0,
    "next7Days": 0,
    "next14Days": 0,
    "next30Days": 0
  }
  ```

---

## 7. Calendar Module

### `GET /calendar/events`

- **Query Params:** `start_date` (YYYY-MM-DD, required), `end_date` (YYYY-MM-DD, required)
- **Response:** `CalendarEvent[]`

---

## Response Models Reference

### User
```json
{
  "id": "uuid",
  "email": "string",
  "display_name": "string | null",
  "role": "admin | sales_rep | viewer",
  "is_active": true,
  "created_at": "ISO 8601",
  "updated_at": "ISO 8601"
}
```

### Customer
```json
{
  "id": "uuid",
  "name": "string",
  "company": "string | null",
  "email": "string | null",
  "phone": "string | null",
  "address": "string | null",
  "notes": "string | null",
  "created_at": "ISO 8601",
  "updated_at": "ISO 8601"
}
```

### Product
```json
{
  "id": "uuid",
  "sku": "string",
  "name": "string",
  "description": "string | null",
  "price": 0.0,
  "stock": 0,
  "min_stock": 0,
  "status": "active | inactive",
  "created_at": "ISO 8601",
  "updated_at": "ISO 8601"
}
```

### Sale
```json
{
  "id": "uuid",
  "customer_id": "uuid",
  "user_id": "uuid",
  "date": "YYYY-MM-DD",
  "total": 0.0,
  "is_payment_pending": false,
  "items": [
    {
      "id": "uuid",
      "product_id": "uuid",
      "quantity": 2,
      "unit_price": 15.5,
      "subtotal": 31.0
    }
  ],
  "created_at": "ISO 8601",
  "updated_at": "ISO 8601"
}
```

### FollowUp
```json
{
  "customer_id": "uuid",
  "customer": "string",
  "email": "string | null",
  "status": "overdue | urgent | upcoming | normal",
  "items": [
    {
      "product_id": "uuid",
      "product_name": "string",
      "avg_interval_days": 30,
      "last_purchase_date": "YYYY-MM-DD",
      "last_quantity": 5,
      "estimated_next_purchase": "YYYY-MM-DD",
      "days_until": 5,
      "current_stock": 100,
      "min_stock": 10,
      "stock_alert": false
    }
  ]
}
```

### FollowUpMetrics
```json
{
  "overdue": 0,
  "next7Days": 0,
  "next14Days": 0,
  "next30Days": 0
}
```

### CalendarEvent
```json
{
  "date": "YYYY-MM-DD",
  "customerId": "uuid",
  "customer": "string",
  "productId": "uuid",
  "productName": "string",
  "type": "overdue | upcoming"
}
```

### DashboardSummary
```json
{
  "totalCustomers": 0,
  "salesThisMonth": 0.0,
  "pendingFollowUps": 0,
  "upcomingPurchases7Days": 0,
  "recentSales": [ /* Sale[] */ ],
  "priorityCustomers": [ /* FollowUp[] */ ]
}
```
