import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.normalize import infer_product_type


def test_infer_product_type_detects_common_product_categories():
    assert infer_product_type("Desk Lamp") == "Lighting"
    assert infer_product_type("Coffee Mug") == "Kitchenware"
    assert infer_product_type("Leather Wallet") == "Accessories"
