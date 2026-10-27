import uuid

from fastapi import APIRouter, Depends, status
from sqlalchemy.orm import Session

from app.api.dependencies import get_current_user, get_db, require_role
from app.models.user import User
from app.schemas.business import BusinessSignUp
from app.schemas.user import (
    PasswordLinkResponse,
    SellerCreate,
    SellerCreatedResponse,
    UserResponse,
    UserUpdate,
    UserUpdateRole,
)
from app.services.user_service import UserService

router = APIRouter(prefix="/auth", tags=["Auth"])


@router.post(
    "/signup", response_model=UserResponse, status_code=status.HTTP_201_CREATED
)
def signup(
    data: BusinessSignUp,
    db: Session = Depends(get_db),
):
    """Registra un comercio y a su admin (Firebase + BD local)."""
    return UserService(db).signup_business(data)


@router.get("/me", response_model=UserResponse)
def get_me(current_user: User = Depends(get_current_user)):
    """Returns the authenticated user's profile (auto-creates on first call)."""
    return current_user


@router.get("/users", response_model=list[UserResponse])
def list_users(
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role("admin")),
):
    """List all users. Admin only."""
    service = UserService(db)
    return service.get_all(current_user)


@router.get("/users/{user_id}", response_model=UserResponse)
def get_user(
    user_id: uuid.UUID,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role("admin")),
):
    """Get a specific user by ID. Admin only."""
    service = UserService(db)
    return service.get_by_id(user_id, current_user)


@router.put("/users/{user_id}", response_model=UserResponse)
def update_user(
    user_id: uuid.UUID,
    data: UserUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role("admin")),
):
    """Update a user (display_name, is_active, role). Admin only."""
    service = UserService(db)
    return service.update(user_id, data, current_user)


@router.patch("/users/{user_id}/role", response_model=UserResponse)
def update_user_role(
    user_id: uuid.UUID,
    data: UserUpdateRole,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role("admin")),
):
    """Change a user's role. Admin only."""
    service = UserService(db)
    return service.update_role(user_id, data.role, current_user)


@router.post(
    "/users", response_model=SellerCreatedResponse, status_code=status.HTTP_201_CREATED
)
def create_seller(
    data: SellerCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role("admin")),
):
    """Da de alta a un vendedor sin contrasena y devuelve su link para definirla."""
    user, link = UserService(db).create_seller(data, current_user)
    return SellerCreatedResponse(
        **UserResponse.model_validate(user).model_dump(), password_setup_link=link
    )


@router.post("/users/{user_id}/password-link", response_model=PasswordLinkResponse)
def create_password_link(
    user_id: uuid.UUID,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role("admin")),
):
    """Genera un nuevo link para que un usuario del comercio defina su contrasena."""
    link = UserService(db).password_link(user_id, current_user)
    return PasswordLinkResponse(password_setup_link=link)
