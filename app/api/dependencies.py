from typing import Generator
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from sqlalchemy.orm import Session
from app.core.database import SessionLocal
from app.core.security import verify_firebase_token
from app.models.user import User
from app.services.user_service import UserService

# HTTP Bearer scheme for extracting the token from the Authorization header
bearer_scheme = HTTPBearer()


def get_db() -> Generator[Session, None, None]:
    """Dependency that provides a SQLAlchemy database session per request.
    
    Yields a session and ensures it is closed after the request completes.
    """
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def get_current_user(
    credentials: HTTPAuthorizationCredentials = Depends(bearer_scheme),
    db: Session = Depends(get_db),
) -> User:
    """Validate Firebase token and return the local User row.
    
    On first call for a given Firebase user, a local DB record is created
    with the default role 'viewer'.
    """
    try:
        token = credentials.credentials
        decoded = verify_firebase_token(token)
        service = UserService(db)
        return service.get_or_create_from_firebase(decoded)
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=f"Invalid authentication credentials: {str(e)}",
        )


def require_role(*allowed_roles: str):
    """Dependency factory that restricts access to specific roles.
    
    Usage:
        @router.get("/admin-only", dependencies=[Depends(require_role("admin"))])
    """
    def role_checker(current_user: User = Depends(get_current_user)) -> User:
        if current_user.role not in allowed_roles:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Role '{current_user.role}' is not allowed. Required: {', '.join(allowed_roles)}",
            )
        return current_user
    return role_checker
