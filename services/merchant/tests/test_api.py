"""
Comprehensive API tests for the merchant catalog backend.

Tests cover:
- Upload workflows with various CSV formats
- List operations and the raw-row contract the agent reads
- Product updates
- Error handling and edge cases
"""
import io
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.main import app

client = TestClient(app)


class TestHealthcheck:
    """Health and basic connectivity tests."""

    def test_health_endpoint(self):
        """Test that healthcheck endpoint returns 200."""
        response = client.get("/health")
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "ok"


class TestUploadWorkflows:
    """CSV upload workflow tests."""

    def test_upload_basic_csv(self):
        """Test basic CSV upload with required fields only."""
        csv = """id,title,price
        P1,Product 1,10.00
        P2,Product 2,20.00"""
        
        response = self._upload_csv(csv, "merchant-1")
        assert response.status_code == 200
        data = response.json()
        assert data["report"]["ok"] is True
        assert data["report"]["rows_out"] == 2
        assert len(data["products"]) == 2

    def test_upload_with_categories(self):
        """Test CSV upload with category inference."""
        csv = """id,title,price,category
        S1,Sunglasses Pro,99.99,Sunglasses
        B1,Blue Light Blocker,45.00,
        L1,Desk Lamp,35.00,"""
        
        response = self._upload_csv(csv, "merchant-2")
        assert response.status_code == 200
        data = response.json()
        products = data["products"]
        
        # Verify categories
        categories = {p["id"]: p["category"] for p in products}
        assert categories["S1"] == "Sunglasses"
        assert categories["B1"] == "Blue Light Glasses"  # Inferred from title
        assert categories["L1"] == "Lighting"  # Inferred from title

    def test_upload_with_missing_stock(self):
        """Test that missing stock defaults to 1 with warning."""
        csv = """id,title,price
        P1,Product,10.00"""
        
        response = self._upload_csv(csv, "merchant-3")
        data = response.json()
        assert data["report"]["rows_out"] == 1
        # Should have stock warning
        assert any("Stock column missing" in w for w in data["report"]["warnings"])
        assert data["products"][0]["stock"] == 1

    def test_upload_with_missing_image_url(self):
        """Test that missing image_url produces warning but upload succeeds."""
        csv = """id,title,price
        P1,Product,10.00"""
        
        response = self._upload_csv(csv, "merchant-4")
        data = response.json()
        assert data["report"]["ok"] is True
        # Should have image warning
        assert any("Image URL missing" in w for w in data["report"]["warnings"])
        assert data["products"][0]["image_url"] is None

    def test_upload_with_header_aliases(self):
        """Test that various header names are aliased correctly."""
        csv = """product_code,product_name,cost
        P1,Product 1,15.00
        P2,Product 2,25.00"""
        
        response = self._upload_csv(csv, "merchant-5")
        data = response.json()
        assert data["report"]["ok"] is True
        assert data["report"]["aliased_columns"]["product_code"] == "id"
        assert data["report"]["aliased_columns"]["product_name"] == "title"
        assert data["report"]["aliased_columns"]["cost"] == "price"

    def test_upload_missing_required_field(self):
        """Test that upload fails gracefully when required field is missing."""
        csv = """id,title
        P1,Product 1"""  # Missing price
        
        response = self._upload_csv(csv, "merchant-6")
        # Should fail with 422
        assert response.status_code == 422
        data = response.json()
        assert "price" in data["report"]["missing_required_columns"]

    def test_upload_with_extra_columns(self):
        """Test that extra columns are captured in attributes."""
        csv = """id,title,price,material,brand
        P1,Product,10.00,Plastic,BrandX"""
        
        response = self._upload_csv(csv, "merchant-7")
        data = response.json()
        assert data["report"]["ok"] is True
        assert set(data["report"]["unmapped_columns"]) == {"material", "brand"}
        # Attributes should contain extra columns
        assert data["products"][0]["attributes"]["material"] == "Plastic"
        assert data["products"][0]["attributes"]["brand"] == "BrandX"

    def test_upload_with_invalid_price(self):
        """Test that rows with invalid prices are skipped."""
        csv = """id,title,price
        P1,Product 1,invalid
        P2,Product 2,20.00"""
        
        response = self._upload_csv(csv, "merchant-8")
        data = response.json()
        assert data["report"]["rows_in"] == 2
        assert data["report"]["rows_out"] == 1  # Only P2
        assert len(data["report"]["skipped_row_details"]) == 1

    def test_upload_large_dataset(self):
        """Test upload of moderately large CSV (100 rows)."""
        rows = ["id,title,price"]
        for i in range(100):
            rows.append(f"P{i},Product {i},{10.00 + i}")
        csv = "\n".join(rows)
        
        response = self._upload_csv(csv, "merchant-9")
        data = response.json()
        assert data["report"]["ok"] is True
        assert data["report"]["rows_out"] == 100

    def _upload_csv(self, csv_content: str, merchant_id: str):
        """Helper to upload CSV."""
        files = {
            "file": ("test.csv", io.BytesIO(csv_content.encode())),
        }
        data = {"merchant_id": merchant_id}
        return client.post("/catalog/upload", files=files, data=data)


class TestListOperations:
    """Product list and retrieval tests."""

    def test_list_empty_merchant(self):
        """Test listing products for merchant with no catalog."""
        response = client.get("/catalog?merchant_id=nonexistent")
        assert response.status_code == 200
        assert response.json() == []

    def test_list_after_upload(self):
        """Test listing products after upload."""
        csv = """id,title,price
        P1,Product 1,10.00
        P2,Product 2,20.00"""
        
        upload_resp = self._upload_csv(csv, "list-test-1")
        assert upload_resp.status_code == 200
        
        list_resp = client.get("/catalog?merchant_id=list-test-1")
        assert list_resp.status_code == 200
        products = list_resp.json()
        assert len(products) == 2
        assert any(p["id"] == "P1" for p in products)

    def _upload_csv(self, csv_content: str, merchant_id: str):
        """Helper to upload CSV."""
        files = {
            "file": ("test.csv", io.BytesIO(csv_content.encode())),
        }
        data = {"merchant_id": merchant_id}
        return client.post("/catalog/upload", files=files, data=data)


class TestRawRowContract:
    """The agent's view: raw rows, served untouched. See docs/CONTRACTS.md §1.1.

    Replaces the old TestSearchOperations — GET /catalog/search was retired in phase 4
    because the agent owns retrieval and nothing else called it.
    """

    def setup_method(self):
        self.merchant_id = "raw-test"
        # Deliberately awkward column names: spaces, casing, punctuation, a currency
        # symbol in the value. All of it must survive the round trip byte-for-byte.
        csv = """Product Code,Product Name,RRP inc VAT,qty_on_hand
SKU1,Widget One,£129.00,4
SKU2,Widget Two,£99.50,0
SKU3,Widget Three,£75.00,12
"""
        files = {"file": ("stock_export.csv", io.BytesIO(csv.encode()))}
        client.post("/catalog/upload", files=files, data={"merchant_id": self.merchant_id})

    def test_raw_rows_preserve_columns_verbatim(self):
        response = client.get(f"/catalog/raw?merchant_id={self.merchant_id}")
        assert response.status_code == 200
        body = response.json()
        assert body["row_count"] == 3
        assert list(body["rows"][0].keys()) == [
            "Product Code", "Product Name", "RRP inc VAT", "qty_on_hand",
        ]

    def test_raw_values_are_not_coerced(self):
        """The agent's coerce step needs the original text to find currency and units."""
        body = client.get(f"/catalog/raw?merchant_id={self.merchant_id}").json()
        assert body["rows"][0]["RRP inc VAT"] == "£129.00"
        assert body["rows"][0]["qty_on_hand"] == "4"

    def test_id_column_is_detected(self):
        body = client.get(f"/catalog/raw?merchant_id={self.merchant_id}").json()
        assert body["id_column"] == "Product Code"

    def test_ids_returns_exactly_those_rows(self):
        """Finding F: without this, pre-charge reverification verifies nothing."""
        response = client.get(f"/catalog/raw?merchant_id={self.merchant_id}&ids=SKU1,SKU3")
        assert response.status_code == 200
        body = response.json()
        assert body["row_count"] == 2
        assert [r["Product Code"] for r in body["rows"]] == ["SKU1", "SKU3"]

    def test_ids_ignores_unknown_ids(self):
        body = client.get(
            f"/catalog/raw?merchant_id={self.merchant_id}&ids=SKU1,NOPE"
        ).json()
        assert [r["Product Code"] for r in body["rows"]] == ["SKU1"]

    def test_unknown_merchant_is_404(self):
        """404, not an empty catalog — those mean different things."""
        assert client.get("/catalog/raw?merchant_id=nobody").status_code == 404

    def test_merchants_lists_stored_catalogs(self):
        response = client.get("/merchants")
        assert response.status_code == 200
        entry = next(
            m for m in response.json()["merchants"] if m["merchant_id"] == self.merchant_id
        )
        assert entry["row_count"] == 3
        assert entry["id_column"] == "Product Code"

    def test_health_reports_storage_mode(self):
        body = client.get("/health").json()
        assert body["storage"] in {"postgres", "memory"}
        assert body["merchants"] >= 1


class TestUpdateOperations:
    """Product update and patch tests."""

    def setup_method(self):
        """Set up test data before each test."""
        self.merchant_id = "update-test"
        csv = """id,title,price,stock
        P1,Original Title,10.00,5"""
        
        files = {
            "file": ("test.csv", io.BytesIO(csv.encode())),
        }
        data = {"merchant_id": self.merchant_id}
        client.post("/catalog/upload", files=files, data=data)

    def test_patch_product_title(self):
        """Test updating product title via PATCH."""
        update_data = {"title": "Updated Title"}
        response = client.patch(
            f"/catalog/P1?merchant_id={self.merchant_id}",
            json=update_data,
        )
        assert response.status_code == 200
        product = response.json()
        assert product["title"] == "Updated Title"

    def test_patch_product_price(self):
        """Test updating product price via PATCH."""
        update_data = {"price": 15.00}
        response = client.patch(
            f"/catalog/P1?merchant_id={self.merchant_id}",
            json=update_data,
        )
        assert response.status_code == 200
        product = response.json()
        assert product["price"] == 15.00

    def test_patch_product_stock(self):
        """Test updating product stock via PATCH."""
        update_data = {"stock": 10}
        response = client.patch(
            f"/catalog/P1?merchant_id={self.merchant_id}",
            json=update_data,
        )
        assert response.status_code == 200
        product = response.json()
        assert product["stock"] == 10

    def test_patch_nonexistent_product(self):
        """Test patching non-existent product returns 404."""
        update_data = {"title": "New Title"}
        response = client.patch(
            f"/catalog/NONEXISTENT?merchant_id={self.merchant_id}",
            json=update_data,
        )
        assert response.status_code == 404


class TestErrorHandling:
    """Error handling and edge case tests."""

    def test_upload_empty_csv(self):
        """Test uploading empty CSV."""
        csv = "id,title,price"  # Header only
        
        files = {
            "file": ("test.csv", io.BytesIO(csv.encode())),
        }
        data = {"merchant_id": "error-test-1"}
        response = client.post("/catalog/upload", files=files, data=data)
        # Empty upload should still succeed with 0 rows
        assert response.status_code == 200
        assert response.json()["report"]["rows_out"] == 0

    def test_upload_malformed_csv(self):
        """Test uploading malformed CSV (inconsistent columns)."""
        csv = """id,title,price
        P1,Product,10.00,Extra
        P2,Product,20.00"""  # Inconsistent number of columns
        
        files = {
            "file": ("test.csv", io.BytesIO(csv.encode())),
        }
        data = {"merchant_id": "error-test-2"}
        response = client.post("/catalog/upload", files=files, data=data)
        # Should handle gracefully
        assert response.status_code == 200

    def test_upload_without_merchant_id(self):
        """Test upload without merchant_id parameter."""
        csv = """id,title,price
        P1,Product,10.00"""
        
        files = {
            "file": ("test.csv", io.BytesIO(csv.encode())),
        }
        response = client.post("/catalog/upload", files=files)
        # Should fail with validation error
        assert response.status_code == 422

    def test_raw_without_merchant_id(self):
        """merchant_id is required — FastAPI rejects the request before the handler."""
        assert client.get("/catalog/raw").status_code == 422

    def test_search_endpoint_is_retired(self):
        """GET /catalog/search was removed in phase 4; the agent owns retrieval.

        405 rather than 404 because PATCH /catalog/{product_id} still matches the path.
        """
        assert client.get("/catalog/search?merchant_id=x").status_code in (404, 405)
