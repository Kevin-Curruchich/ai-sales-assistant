from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session
from app.api.dependencies import get_db, get_current_user
from app.schemas.sale import PaginatedFollowUpResponse, FollowUpMetrics
from app.services.sale_service import SaleService

router = APIRouter(prefix="/follow-ups", tags=["Follow-ups"])


@router.get("", response_model=PaginatedFollowUpResponse)
def list_follow_ups(
    filter: str = "all",
    limit: int = 10,
    offset: int = 0,
    db: Session = Depends(get_db),
    _current_user: dict = Depends(get_current_user),
):
    """Get follow-up list with pagination.
    
    filter options: all, overdue, 7_days, 14_days, 30_days
    """
    service = SaleService(db)
    items, total = service.get_follow_ups(filter_type=filter, limit=limit, offset=offset)
    return {"data": items, "meta": {"total": total}}


@router.get("/metrics", response_model=FollowUpMetrics)
def get_follow_up_metrics(
    db: Session = Depends(get_db),
    _current_user: dict = Depends(get_current_user),
):
    service = SaleService(db)
    return service.get_follow_up_metrics()
