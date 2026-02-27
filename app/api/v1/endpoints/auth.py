import uuid
from typing import Optional
from fastapi import APIRouter, Depends, status
from sqlalchemy.orm import Session
from app.api.dependencies import get_db, get_current_user, require_role
from app.models.user import User
from app.schemas.user import UserResponse, UserUpdate, UserUpdateRole, UserSignUp
from app.services.user_service import UserService

router = APIRouter(prefix="/auth", tags=["Auth"])


@router.post("/signup", response_model=UserResponse, status_code=status.HTTP_201_CREATED)
def signup(
    data: UserSignUp,
    db: Session = Depends(get_db),
):
    """Register a new user.
    
    Creates a Firebase Authentication user with custom claims (role, is_active)
    and a corresponding record in the local database.
    """
    service = UserService(db)
    return service.signup(data)


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
    return service.get_all()


@router.get("/users/{user_id}", response_model=UserResponse)
def get_user(
    user_id: uuid.UUID,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role("admin")),
):
    """Get a specific user by ID. Admin only."""
    service = UserService(db)
    return service.get_by_id(user_id)


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
