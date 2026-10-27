import uuid

from fastapi import HTTPException, status
from sqlalchemy.orm import Session

from app.core.security import (
    create_firebase_user,
    delete_firebase_user,
    generate_password_setup_link,
    set_firebase_custom_claims,
)
from app.models.business import Business
from app.models.user import User
from app.repositories.user_repository import UserRepository
from app.schemas.business import BusinessSignUp
from app.schemas.user import SellerCreate, UserUpdate


class UserService:
    def __init__(self, db: Session):
        self.db = db
        self.repo = UserRepository(db)

    def get_for_firebase(self, decoded_token: dict) -> User:
        """Busca el usuario local por UID de Firebase (= User.id).

        Ya no se crea al vuelo: un usuario sin fila no pertenece a ningun
        comercio, asi que se rechaza con 403.
        """
        user = self.repo.get_by_id(decoded_token["uid"])
        if not user:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Tu cuenta no pertenece a ningun comercio",
            )
        return user

    def _check_email_free(self, email: str) -> None:
        if self.repo.get_by_email(email):
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"A user with email '{email}' already exists",
            )

    def _create_in_firebase_and_db(
        self, user: User, password: str | None, extra=None
    ) -> User:
        """Crea el usuario en Firebase (con claims) y su fila local.

        Si algo falla despues de crearlo en Firebase, lo borra y relanza para
        no dejar una cuenta huerfana.
        """
        create_firebase_user(
            uid=str(user.id),
            email=user.email,
            password=password,
            display_name=user.display_name,
        )
        try:
            set_firebase_custom_claims(
                str(user.id), {"role": user.role, "is_active": True}
            )
            if extra:
                extra()
            return self.repo.create(user)
        except Exception:
            self.db.rollback()
            delete_firebase_user(str(user.id))
            raise

    def signup_business(self, data: BusinessSignUp) -> User:
        """Alta publica: crea el comercio y su admin en una sola transaccion.

        Corre antes de que haya comercio ligado, asi que el `business_id` del
        usuario va explicito. El rol siempre es admin.
        """
        self._check_email_free(data.email)
        business = Business(name=data.business_name, timezone=data.timezone)
        self.db.add(business)
        self.db.flush()
        user = User(
            id=uuid.uuid4(),
            email=data.email,
            display_name=data.display_name,
            role="admin",
            business_id=business.id,
        )
        return self._create_in_firebase_and_db(user, data.password)

    def create_seller(self, data: SellerCreate, admin: User) -> tuple[User, str]:
        """El admin da de alta a un vendedor sin contrasena; devuelve el link
        de Firebase para que la defina."""
        self._check_email_free(data.email)
        user = User(
            id=uuid.uuid4(),
            email=data.email,
            display_name=data.display_name,
            role=data.role,
            business_id=admin.business_id,
        )
        link: list[str] = []
        user = self._create_in_firebase_and_db(
            user, None, lambda: link.append(generate_password_setup_link(data.email))
        )
        return user, link[0]

    def password_link(self, user_id: uuid.UUID, admin: User) -> str:
        user = self.get_by_id(user_id, admin)
        return generate_password_setup_link(user.email)

    def get_by_id(self, user_id: uuid.UUID, admin: User) -> User:
        # `users` no tiene RLS: el filtro por comercio va aqui. Uno ajeno es 404.
        user = self.repo.get_by_id(user_id)
        if not user or user.business_id != admin.business_id:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"User with id {user_id} not found",
            )
        return user

    def get_all(self, admin: User) -> list[User]:
        return self.repo.get_all(admin.business_id)

    def update_role(self, user_id: uuid.UUID, role: str, admin: User) -> User:
        user = self.get_by_id(user_id, admin)
        user.role = role
        set_firebase_custom_claims(
            str(user.id), {"role": role, "is_active": user.is_active}
        )
        return self.repo.update(user)

    def update(self, user_id: uuid.UUID, data: UserUpdate, admin: User) -> User:
        user = self.get_by_id(user_id, admin)
        update_data = data.model_dump(exclude_unset=True)
        for key, value in update_data.items():
            setattr(user, key, value)
        if "role" in update_data or "is_active" in update_data:
            set_firebase_custom_claims(
                str(user.id), {"role": user.role, "is_active": user.is_active}
            )
        return self.repo.update(user)
