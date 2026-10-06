from datetime import date
from fastapi import APIRouter, Depends, HTTPException, status
from app.api.dependencies import SaleServiceDep, get_current_user
from app.schemas.sale import CalendarResponse

router = APIRouter(
    prefix="/calendar",
    tags=["Calendar"],
    dependencies=[Depends(get_current_user)],
)


@router.get("/events", response_model=CalendarResponse)
def get_calendar_events(
    start_date: date,
    end_date: date,
    service: SaleServiceDep,
):
    try:
        return service.get_calendar_events(start_date=start_date, end_date=end_date)
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"An error occurred while fetching calendar events: {str(e)}",
        )
