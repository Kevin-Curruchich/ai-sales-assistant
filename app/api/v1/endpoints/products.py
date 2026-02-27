import uuid
from typing import Optional
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session
from app.api.dependencies import get_db, get_current_user
from app.schemas.product import ProductCreate, ProductUpdate, ProductResponse, PaginatedProductResponse
from app.services.product_service import ProductService

router = APIRouter(prefix="/products", tags=["Products"])


@router.get("", response_model=PaginatedProductResponse)
def list_products(
    search: Optional[str] = None,
    status_filter: Optional[str] = None,
    limit: int = 10,
    offset: int = 0,
    db: Session = Depends(get_db),
    _current_user: dict = Depends(get_current_user),
):
    try:
        service = ProductService(db)
        items = service.get_all(search=search, status_filter=status_filter, limit=limit, offset=offset)
        total = service.count(search=search, status_filter=status_filter)
        return {"data": items, "meta": {"total": total}}
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"An error occurred while listing products: {str(e)}",
        )


@router.get("/{product_id}", response_model=ProductResponse)
def get_product(
    product_id: uuid.UUID,
    db: Session = Depends(get_db),
    _current_user: dict = Depends(get_current_user),
):
    try:
        service = ProductService(db)
        return service.get_by_id(product_id)
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Product not found: {str(e)}",
        )


@router.post("", response_model=ProductResponse, status_code=status.HTTP_201_CREATED)
def create_product(
    data: ProductCreate,
    db: Session = Depends(get_db),
    _current_user: dict = Depends(get_current_user),
):
    try:
        service = ProductService(db)
        return service.create(data)
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"An error occurred while creating the product: {str(e)}",
        )


@router.put("/{product_id}", response_model=ProductResponse)
def update_product(
    product_id: uuid.UUID,
    data: ProductUpdate,
    db: Session = Depends(get_db),
    _current_user: dict = Depends(get_current_user),
):
    try:
        service = ProductService(db)
        return service.update(product_id, data)
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"An error occurred while updating the product: {str(e)}",
        )


@router.delete("/{product_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_product(
    product_id: uuid.UUID,
    db: Session = Depends(get_db),
    _current_user: dict = Depends(get_current_user),
):
    try:
        service = ProductService(db)
        service.delete(product_id)
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"An error occurred while deleting the product: {str(e)}",
        )
