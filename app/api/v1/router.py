from fastapi import APIRouter
from app.api.v1.endpoints import auth, dashboard, products, customers, sales, follow_ups, calendar, purchases

api_router = APIRouter()

api_router.include_router(auth.router)
api_router.include_router(dashboard.router)
api_router.include_router(products.router)
api_router.include_router(customers.router)
api_router.include_router(sales.router)
api_router.include_router(follow_ups.router)
api_router.include_router(calendar.router)
api_router.include_router(purchases.router)
