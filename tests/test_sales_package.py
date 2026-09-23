from pathlib import Path


def test_sale_service_is_importable_from_the_package():
    from app.services.sales import SaleService

    assert SaleService.__name__ == "SaleService"


def test_the_old_module_is_gone():
    """Un shim dejaria dos rutas de import para la misma clase."""
    repo_root = Path(__file__).resolve().parent.parent
    assert not (repo_root / "app" / "services" / "sale_service.py").exists()


def test_nothing_imports_the_old_path():
    repo_root = Path(__file__).resolve().parent.parent
    offenders = [
        str(path.relative_to(repo_root))
        for path in (repo_root / "app").rglob("*.py")
        if "from app.services.sale_service import" in path.read_text()
    ]
    assert offenders == [], f"Todavia importan la ruta vieja: {offenders}"
