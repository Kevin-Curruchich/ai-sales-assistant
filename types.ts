// ============================================================
// Revenew — TypeScript Interfaces
// Generated from the FastAPI backend schemas.
// Drop this file into your React project (e.g. src/types/api.ts)
// ============================================================

// All IDs are UUID v4 strings (e.g. "a3f1b2c4-5d6e-7f8a-9b0c-1d2e3f4a5b6c")

// -------------------------------------------------------
// User
// -------------------------------------------------------

export type UserRole = "admin" | "sales_rep" | "viewer";

export interface User {
  id: string; // UUID — same value used as Firebase UID
  email: string;
  display_name: string | null;
  role: UserRole;
  is_active: boolean;
  created_at: string; // ISO 8601
  updated_at: string; // ISO 8601
}

export interface UserSignUpRequest {
  email: string;
  password: string;
  display_name?: string | null;
  role?: UserRole; // default: "viewer"
}

export interface UserUpdateRequest {
  display_name?: string | null;
  is_active?: boolean | null;
  role?: UserRole | null;
}

export interface UserUpdateRoleRequest {
  role: UserRole;
}

// -------------------------------------------------------
// Customer
// -------------------------------------------------------

export interface Customer {
  id: string; // UUID
  name: string;
  company: string | null;
  email: string | null;
  phone: string | null;
  address: string | null;
  notes: string | null;
  created_at: string;
  updated_at: string;
}

export interface CustomerCreateRequest {
  name: string;
  company?: string | null;
  email?: string | null;
  phone?: string | null;
  address?: string | null;
  notes?: string | null;
}

export interface CustomerUpdateRequest {
  name?: string | null;
  company?: string | null;
  email?: string | null;
  phone?: string | null;
  address?: string | null;
  notes?: string | null;
}

// -------------------------------------------------------
// Product
// -------------------------------------------------------

export type ProductStatus = "active" | "inactive";

export interface Product {
  id: string; // UUID
  sku: string;
  name: string;
  description: string | null;
  price: number;
  stock: number;
  min_stock: number;
  status: ProductStatus;
  created_at: string;
  updated_at: string;
}

export interface ProductCreateRequest {
  sku: string;
  name: string;
  description?: string | null;
  price: number;
  stock?: number;      // default: 0
  min_stock?: number;   // default: 0
  status?: ProductStatus; // default: "active"
}

export interface ProductUpdateRequest {
  sku?: string | null;
  name?: string | null;
  description?: string | null;
  price?: number | null;
  stock?: number | null;
  min_stock?: number | null;
  status?: ProductStatus | null;
}

// -------------------------------------------------------
// Sale
// -------------------------------------------------------

export interface SaleItem {
  id: string; // UUID
  product_id: string; // UUID
  quantity: number;
  unit_price: number;
  subtotal: number;
}

export interface Sale {
  id: string; // UUID
  customer_id: string; // UUID
  user_id: string; // UUID
  date: string; // YYYY-MM-DD
  total: number;
  items: SaleItem[];
  created_at: string;
  updated_at: string;
}

export interface SaleItemCreateRequest {
  productId: string; // UUID
  quantity: number;
  unitPrice: number;
}

export interface SaleCreateRequest {
  customerId: string; // UUID
  date: string; // YYYY-MM-DD
  items: SaleItemCreateRequest[];
}

export interface SaleUpdateRequest {
  customerId?: string | null; // UUID
  date?: string | null;
  items?: SaleItemCreateRequest[] | null;
}

// -------------------------------------------------------
// Follow-ups
// -------------------------------------------------------

export type FollowUpStatus = "overdue" | "urgent" | "upcoming" | "normal";

export interface FollowUpItem {
  product_id: string; // UUID
  product_name: string;
  avg_interval_days: number | null;
  last_purchase_date: string | null; // YYYY-MM-DD
  last_quantity: number;
  estimated_next_purchase: string | null; // YYYY-MM-DD
  days_until: number | null;
  current_stock: number;
  min_stock: number;
  stock_alert: boolean;
}

export interface FollowUp {
  customer_id: string; // UUID
  customer: string;
  email: string | null;
  status: FollowUpStatus;
  items: FollowUpItem[];
}

export interface FollowUpMetrics {
  overdue: number;
  next7Days: number;
  next14Days: number;
  next30Days: number;
}

export type FollowUpFilter = "all" | "overdue" | "7_days" | "14_days" | "30_days";

// -------------------------------------------------------
// Calendar
// -------------------------------------------------------

export type CalendarEventType = "overdue" | "upcoming";

export interface CalendarEvent {
  date: string; // YYYY-MM-DD
  customerId: string; // UUID
  customer: string;
  productId: string; // UUID
  productName: string;
  type: CalendarEventType;
}

// -------------------------------------------------------
// Dashboard
// -------------------------------------------------------

export interface DashboardSummary {
  totalCustomers: number;
  salesThisMonth: number;
  pendingFollowUps: number;
  upcomingPurchases7Days: number;
  recentSales: Sale[];
  priorityCustomers: FollowUp[];
}
