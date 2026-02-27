import uuid
from typing import Optional
from fastapi import HTTPException, status
from sqlalchemy.orm import Session
from app.models.user import User
from app.repositories.user_repository import UserRepository
from app.schemas.user import UserUpdate, UserSignUp
from app.core.security import create_firebase_user, set_firebase_custom_claims


class UserService:
    def __init__(self, db: Session):
        self.repo = UserRepository(db)

    def get_or_create_from_firebase(self, decoded_token: dict) -> User:
        """Find the local user by Firebase UID (= User.id), or create one on first login.
        
        This is called automatically on every authenticated request so the
        database always has a record for the calling user.
        """
        try:
            user = self.repo.get_by_id(decoded_token["uid"])
            if not user:
                user = User(
                    id=decoded_token["uid"],
                    email=decoded_token.get("email"),
                    display_name=decoded_token.get("name"),
                )
                self.repo.create(user)
            return user
        except Exception as e:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"An error occurred while processing the user: {str(e)}",
            )

    def signup(self, data: UserSignUp) -> User:
        """Create a Firebase user with custom claims and a local DB record.
        
        1. Generates a UUID locally.
        2. Creates the user in Firebase Authentication using that UUID as the uid.
        3. Sets custom claims (role, is_active) on the Firebase user.
        4. Creates a local DB record with the same UUID as its primary key.
        """
        # Check if email already exists locally
        existing = self.repo.get_by_email(data.email)
        if existing:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"A user with email '{data.email}' already exists",
            )

        allowed_roles = {"admin", "sales_rep", "viewer"}
        if data.role not in allowed_roles:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Invalid role. Must be one of: {', '.join(allowed_roles)}",
            )

        # 1. Generate UUID
        user_id = uuid.uuid4()

        # 2. Create user in Firebase with our UUID as the uid
        firebase_user = create_firebase_user(
            uid=str(user_id),
            email=data.email,
            password=data.password,
            display_name=data.display_name,
        )

        # 3. Set custom claims on the Firebase user
        set_firebase_custom_claims(firebase_user.uid, {
            "role": data.role,
            "is_active": True,
        })

        # 4. Create local DB record with the same UUID
        user = User(
            id=user_id,
            email=data.email,
            display_name=data.display_name,
            role=data.role,
        )
        return self.repo.create(user)

    def get_by_id(self, user_id: uuid.UUID) -> User:
        user = self.repo.get_by_id(user_id)
        if not user:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"User with id {user_id} not found",
            )
        return user

    def get_all(self) -> list[User]:
        return self.repo.get_all()

    def update_role(self, user_id: uuid.UUID, role: str, current_user: User) -> User:
        """Only admins can change roles."""
        if current_user.role != "admin":
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Only admins can change user roles",
            )
        allowed_roles = {"admin", "manager", "viewer"}
        if role not in allowed_roles:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Invalid role. Must be one of: {', '.join(allowed_roles)}",
            )
        user = self.get_by_id(user_id)
        user.role = role
        # Sync custom claims to Firebase
        set_firebase_custom_claims(str(user.id), {
            "role": role,
            "is_active": user.is_active,
        })
        return self.repo.update(user)

    def update(self, user_id: uuid.UUID, data: UserUpdate, current_user: User) -> User:
        """Update user fields. Role changes require admin."""
        if data.role is not None and current_user.role != "admin":
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Only admins can change user roles",
            )
        user = self.get_by_id(user_id)
        update_data = data.model_dump(exclude_unset=True)
        for key, value in update_data.items():
            setattr(user, key, value)
        # Sync custom claims to Firebase if role or is_active changed
        if "role" in update_data or "is_active" in update_data:
            set_firebase_custom_claims(str(user.id), {
                "role": user.role,
                "is_active": user.is_active,
            })
        return self.repo.update(user)
