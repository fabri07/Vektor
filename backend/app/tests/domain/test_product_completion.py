from decimal import Decimal

from app.domain.product_completion import recompute_requires_completion
from app.persistence.models.product import Product


def test_recompute_requires_completion_when_price_and_cost_exist() -> None:
    product = Product(
        name="Incompleto",
        sale_price_ars=Decimal("10"),
        unit_cost_ars=Decimal("0"),
        requires_completion=True,
    )
    recompute_requires_completion(product)
    assert product.requires_completion is False
