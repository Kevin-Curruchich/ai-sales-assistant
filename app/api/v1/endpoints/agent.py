import uuid

from fastapi import APIRouter, Depends, status
from sqlalchemy.orm import Session

from app.api.dependencies import get_db, get_current_user
from app.models import User
from app.schemas.agent import AgentThreadCreate, AgentThreadRename, AgentThreadResponse
from app.services.agent_thread_service import AgentThreadService

router = APIRouter(prefix="/agent", tags=["Agente"])

DEFAULT_TITLE = "Conversación nueva"


@router.get("/threads", response_model=list[AgentThreadResponse])
def list_threads(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    service = AgentThreadService(db)
    return service.list_for(current_user.id)


@router.post("/threads", response_model=AgentThreadResponse, status_code=status.HTTP_201_CREATED)
def create_thread(
    data: AgentThreadCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    service = AgentThreadService(db)
    return service.create(current_user.id, data.title or DEFAULT_TITLE)


@router.get("/threads/{thread_id}", response_model=AgentThreadResponse)
def get_thread(
    thread_id: uuid.UUID,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    service = AgentThreadService(db)
    return service.get_owned(thread_id, current_user.id)


@router.patch("/threads/{thread_id}", response_model=AgentThreadResponse)
def rename_thread(
    thread_id: uuid.UUID,
    data: AgentThreadRename,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    service = AgentThreadService(db)
    return service.rename_owned(thread_id, current_user.id, data.title)


@router.delete("/threads/{thread_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_thread(
    thread_id: uuid.UUID,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    service = AgentThreadService(db)
    service.delete_owned(thread_id, current_user.id)
