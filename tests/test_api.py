"""
Comprehensive API tests for the merchant catalog backend.

Tests cover:
- Upload workflows with various CSV formats
- List and search operations
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


class TestChat:
    """Natural-language product discovery tests."""

    def test_chat_applies_llm_attribute_filter(self, monkeypatch):
        """Test arbitrary numeric attributes returned by the LLM are applied."""
        class FakeLLM:
            def parse_search_request(self, message):
                return {
                    "query": "glasses",
                    "in_stock_only": True,
                    "attribute_filters": {"weight": {"max": 20}},
                }

        monkeypatch.setattr("app.main.llm_client", FakeLLM())
        csv = """id,title,price,weight,stock
        P1,Light Glasses,80.00,19.5,4
        P2,Heavy Glasses,90.00,22.0,4"""
        upload = client.post(
            "/catalog/upload",
            files={"file": ("chat.csv", io.BytesIO(csv.encode()))},
            data={"merchant_id": "chat-weight"},
        )
        assert upload.status_code == 200

        response = client.post(
            "/chat",
            json={
                "merchant_id": "chat-weight",
                "message": "glasses weight less than 20g",
            },
        )
        assert response.status_code == 200
        result = response.json()
        assert [product["id"] for product in result["products"]] == ["P1"]
        assert result["filters"]["attribute_filters"] == {"weight": {"max": 20}}

    def test_chat_fallback_searches_attributes_without_openai(self, monkeypatch):
        """Test chat still finds products when OpenAI is unavailable."""
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        csv = """id,title,price,color,frame shape,stock
        P1,Everyday Frame,80.00,Black,Round,4
        P2,Blue Frame,70.00,Blue,Square,4"""
        response = client.post(
            "/catalog/upload",
            files={"file": ("chat.csv", io.BytesIO(csv.encode()))},
            data={"merchant_id": "chat-fallback"},
        )
        assert response.status_code == 200

        response = client.post(
            "/chat",
            json={
                "merchant_id": "chat-fallback",
                "message": "black round",
            },
        )
        assert response.status_code == 200
        result = response.json()
        assert result["products"][0]["id"] == "P1"
        assert result["filters"]["in_stock_only"] is True

    def test_chat_rejects_empty_message(self):
        response = client.post(
            "/chat",
            json={"merchant_id": "chat-empty", "message": ""},
        )
        assert response.status_code == 422

    def test_chat_fallback_handles_shopping_language(self, monkeypatch):
        """Test the exact natural-language fallback without an API key."""
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        csv = """id,title,price,category,frame color,stock
        P1,Everyday Frame,80.00,Sunglasses,Black,4
        P2,Blue Frame,70.00,Sunglasses,Blue,4"""
        upload = client.post(
            "/catalog/upload",
            files={"file": ("chat.csv", io.BytesIO(csv.encode()))},
            data={"merchant_id": "chat-phrase"},
        )
        assert upload.status_code == 200

        response = client.post(
            "/chat",
            json={
                "merchant_id": "chat-phrase",
                "message": "Show me affordable black sunglasses under 150 dollars",
            },
        )
        assert response.status_code == 200
        result = response.json()
        assert [product["id"] for product in result["products"]] == ["P1"]
        assert result["filters"]["max_price"] == 150

    def test_chat_searches_attribute_field_names(self, monkeypatch):
        """Test a request that refers to an attribute field and its value."""
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        csv = """id,title,price,frame width fit,stock
        P1,Lightweight Frame,80.00,Narrow,4
        P2,Wide Frame,90.00,Wide,4"""
        upload = client.post(
            "/catalog/upload",
            files={"file": ("chat.csv", io.BytesIO(csv.encode()))},
            data={"merchant_id": "chat-width"},
        )
        assert upload.status_code == 200

        response = client.post(
            "/chat",
            json={
                "merchant_id": "chat-width",
                "message": "Show me glasses with narrow frame width fit",
            },
        )
        assert response.status_code == 200
        assert [product["id"] for product in response.json()["products"]] == ["P1"]


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


class TestSearchOperations:
    """Product search and filtering tests."""

    def setup_method(self):
        """Set up test data before each test."""
        self.merchant_id = "search-test"
        csv = """id,title,price,category,stock
        S1,Sunglasses Pro,99.99,Sunglasses,5
        S2,Sunglasses Basic,49.99,Sunglasses,0
        B1,Blue Light Filter,45.00,,10
        L1,Desk Lamp,35.00,Lighting,2"""
        
        files = {
            "file": ("test.csv", io.BytesIO(csv.encode())),
        }
        data = {"merchant_id": self.merchant_id}
        client.post("/catalog/upload", files=files, data=data)

    def test_search_by_query(self):
        """Test search by keyword query."""
        response = client.get(
            f"/catalog/search?merchant_id={self.merchant_id}&query=sunglasses"
        )
        assert response.status_code == 200
        results = response.json()
        assert len(results) >= 1
        assert any("Sunglasses" in p["title"] for p in results)

    def test_search_by_category(self):
        """Test search by category."""
        response = client.get(
            f"/catalog/search?merchant_id={self.merchant_id}&category=Sunglasses"
        )
        assert response.status_code == 200
        results = response.json()
        assert len(results) >= 2
        assert all(p["category"] == "Sunglasses" for p in results)

    def test_search_by_price_range(self):
        """Test search by price range."""
        response = client.get(
            f"/catalog/search?merchant_id={self.merchant_id}&min_price=40&max_price=100"
        )
        assert response.status_code == 200
        results = response.json()
        for p in results:
            assert 40 <= p["price"] <= 100

    def test_search_in_stock_only(self):
        """Test search filtered to in-stock items."""
        response = client.get(
            f"/catalog/search?merchant_id={self.merchant_id}&in_stock_only=true"
        )
        assert response.status_code == 200
        results = response.json()
        assert all(p["stock"] > 0 for p in results)

    def test_search_combined_filters(self):
        """Test search with multiple filters combined."""
        response = client.get(
            f"/catalog/search?merchant_id={self.merchant_id}&category=Sunglasses&min_price=50&in_stock_only=true"
        )
        assert response.status_code == 200
        results = response.json()
        for p in results:
            assert p["category"] == "Sunglasses"
            assert p["price"] >= 50
            assert p["stock"] > 0


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

    def test_search_without_merchant_id(self):
        """Test search without merchant_id parameter."""
        response = client.get("/catalog/search")
        # Should fail or return empty
        assert response.status_code in [200, 422]
