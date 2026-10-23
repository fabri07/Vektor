"""Regla canónica para cerrar productos incompletos."""

from app.persistence.models.product import Product


def recompute_requires_completion(product: Product) -> None:
    """Marca completo un producto cuando ya tiene precio de venta y costo."""
    if (
        product.requires_completion
        and product.sale_price_ars
        and product.sale_price_ars > 0
        and product.unit_cost_ars is not None
    ):
        product.requires_completion = False
