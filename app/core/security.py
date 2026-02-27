import firebase_admin
from firebase_admin import auth as firebase_auth, credentials
from fastapi import HTTPException, status
from app.core.config import settings


def initialize_firebase() -> None:
    """Initialize the Firebase Admin SDK.
    
    Call this once at application startup.
    Uses the service account JSON file specified by FIREBASE_CREDENTIALS_PATH,
    or Application Default Credentials if no path is provided.
    """
    if firebase_admin._apps:
        return # Already initialized

    if settings.FIREBASE_CREDENTIALS_PATH:
        cred = credentials.Certificate(settings.FIREBASE_CREDENTIALS_PATH)
        firebase_admin.initialize_app(cred)
    else:
        # Uses GOOGLE_APPLICATION_CREDENTIALS env variable or ADC
        firebase_admin.initialize_app()


def verify_firebase_token(id_token: str) -> dict:
    """Verify a Firebase ID token and return the decoded claims.
    
    Args:
        id_token: The Firebase ID token string from the client.
        
    Returns:
        A dict with the decoded token claims (uid, email, etc.).
        
    Raises:
        HTTPException 401 if the token is invalid or expired.
    """
    try:
        decoded_token = firebase_auth.verify_id_token(id_token)
        return decoded_token
    except firebase_auth.ExpiredIdTokenError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Token has expired",
        )
    except firebase_auth.InvalidIdTokenError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid authentication token",
        )
    except Exception:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Could not validate credentials",
        )


def create_firebase_user(
    uid: str,
    email: str,
    password: str,
    display_name: str | None = None,
) -> firebase_auth.UserRecord:
    """Create a new user in Firebase Authentication with a specific UID.
    
    Raises:
        HTTPException 409 if the email already exists.
        HTTPException 400 for other Firebase errors.
    """
    try:
        user_record = firebase_auth.create_user(
            uid=uid,
            email=email,
            password=password,
            display_name=display_name,
        )
        return user_record
    except firebase_auth.EmailAlreadyExistsError:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"A user with email '{email}' already exists",
        )
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Failed to create Firebase user: {str(e)}",
        )


def set_firebase_custom_claims(uid: str, claims: dict) -> None:
    """Set custom claims (role, is_active) on a Firebase user.
    
    These claims are included in the ID token after the user refreshes it.
    """
    try:
        firebase_auth.set_custom_user_claims(uid, claims)
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to set custom claims: {str(e)}",
        )
