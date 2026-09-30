import uuid

from fastapi import HTTPException
from sqlalchemy.orm import Session

from app.models import AgentThread
from app.repositories.agent_thread_repository import AgentThreadRepository


class AgentThreadService:
    """Casos de uso sobre hilos del agente, con la autorizacion concentrada
    en `get_owned` -- ver su docstring."""

    def __init__(self, db: Session):
        self.db = db
        self.repo = AgentThreadRepository(db)

    def create(self, owner_id: uuid.UUID, title: str) -> AgentThread:
        thread = self.repo.create(owner_id=owner_id, title=title)
        self.db.commit()
        return thread

    def list_for(self, owner_id: uuid.UUID) -> list[AgentThread]:
        return self.repo.list_for_owner(owner_id)

    def get_owned(self, thread_id: uuid.UUID, user_id: uuid.UUID) -> AgentThread:
        """El unico lugar donde se decide si alguien puede tocar un hilo.

        Mismo 404 para "no existe" y para "no es tuyo": un 403 le diria a
        quien pregunta que ese hilo existe y es de otro.

        Cuando se agregue lectura compartida, se agrega ACA y en ningun otro
        lado.
        """
        thread = self.repo.get(thread_id)
        if thread is None or thread.owner_id != user_id:
            raise HTTPException(status_code=404, detail="Hilo no encontrado")
        return thread

    def rename_owned(self, thread_id: uuid.UUID, user_id: uuid.UUID, title: str) -> AgentThread:
        thread = self.get_owned(thread_id, user_id)
        self.repo.rename(thread.id, title)
        self.db.commit()
        self.db.refresh(thread)
        return thread

    def delete_owned(self, thread_id: uuid.UUID, user_id: uuid.UUID) -> None:
        thread = self.get_owned(thread_id, user_id)
        self.repo.delete(thread.id)
        self.db.commit()
