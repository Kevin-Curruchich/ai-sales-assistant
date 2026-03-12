from datetime import date
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session
from app.api.dependencies import get_db, get_current_user
from app.schemas.sale import CalendarResponse
from app.services.sale_service import SaleService

router = APIRouter(prefix="/calendar", tags=["Calendar"])


@router.get("/events", response_model=CalendarResponse)
def get_calendar_events(
    start_date: date,
    end_date: date,
    db: Session = Depends(get_db),
    _current_user: dict = Depends(get_current_user),
):
    try:
        service = SaleService(db)
        return service.get_calendar_events(start_date=start_date, end_date=end_date)
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"An error occurred while fetching calendar events: {str(e)}",
        )
