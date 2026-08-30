"""The console's product list must always describe the sheet that is actually stored.

Every test here is a regression for one way the merchant console showed a merchant a
catalog they no longer had:

- an Excel upload that could not get in at all, so the console kept the last CSV
- a sheet whose required columns did not map, which normalized to nothing and left the
  PREVIOUS upload's products serving from GET /catalog
- a sale that never reached the stored rows, so stock was whatever the spreadsheet said
  on upload day, forever

See docs/CONTRACTS.md §1.3, §1.4 and §1.5.
"""

import io
import sys
from pathlib import Path

import pandas as pd
import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.main import app

client = TestClient(app)


def upload(content: bytes, filename: str, merchant_id: str):
    return client.post(
        "/catalog/upload",
        data={"merchant_id": merchant_id},
        files={"file": (filename, io.BytesIO(content), "application/octet-stream")},
    )


def xlsx(frame: pd.DataFrame) -> bytes:
    buffer = io.BytesIO()
    frame.to_excel(buffer, index=False)
    return buffer.getvalue()


class TestExcelUploads:
    """Merchants keep catalogs in spreadsheets."""

    def test_xlsx_upload_stores_raw_rows(self):
        frame = pd.DataFrame(
            [
                {"sku": "A1", "Product Name": "Widget", "RRP inc VAT": "129.00", "qty": "4"},
                {"sku": "A2", "Product Name": "Gadget", "RRP inc VAT": "59.00", "qty": "0"},
            ]
        )
        response = upload(xlsx(frame), "autumn.xlsx", "xl-1")
        assert response.status_code in (200, 422)

        raw = client.get("/catalog/raw", params={"merchant_id": "xl-1"}).json()
        assert raw["row_count"] == 2
        # Columns survive with their spaces and casing intact — the agent's profiler
        # reads these, and a renamed column is one it cannot recognise.
        assert raw["columns"] == ["sku", "Product Name", "RRP inc VAT", "qty"]
        assert raw["source_filename"] == "autumn.xlsx"
        assert raw["rows"][0]["Product Name"] == "Widget"

    def test_xlsx_values_are_not_coerced(self):
        """A "01234" SKU must not come back as 1234. pandas' type inference is exactly
        the coercion docs/CONTRACTS.md §0 forbids."""
        frame = pd.DataFrame([{"sku": "01234", "title": "Widget", "price": "9.50"}])
        upload(xlsx(frame), "codes.xlsx", "xl-2")

        raw = client.get("/catalog/raw", params={"merchant_id": "xl-2"}).json()
        assert raw["rows"][0]["sku"] == "01234"

    def test_mislabelled_csv_still_parses(self):
        """A spreadsheet exported with the wrong extension is common enough to retry."""
        response = upload(b"id,title,price\nP1,Widget,10.00\n", "catalog.xlsx", "xl-3")
        assert response.status_code in (200, 422)
        assert client.get("/catalog/raw", params={"merchant_id": "xl-3"}).json()["row_count"] == 1

    def test_unreadable_upload_is_a_400(self):
        response = upload(b"\x00\x01\x02not a table at all", "junk.xlsx", "xl-4")
        assert response.status_code == 400


class TestConsoleViewNeverGoesStale:
    """GET /catalog may never describe a sheet that is no longer stored."""

    def test_failed_normalization_clears_the_previous_catalog(self):
        good = b"id,title,price\nP1,First upload,10.00\nP2,Second,20.00\n"
        assert upload(good, "good.csv", "stale-1").status_code == 200
        assert len(client.get("/catalog", params={"merchant_id": "stale-1"}).json()) == 2

        # No id/title/price to map: normalizes to nothing, 422.
        bad = b"reference,heading,cost_ex_vat\nR1,Something,12\n"
        response = upload(bad, "bad.csv", "stale-1")
        assert response.status_code == 422

        # THE REGRESSION: this used to still return the two products from `good.csv`,
        # while /catalog/raw returned the row from `bad.csv`. The console showed one
        # catalog and the agent sold from the other.
        assert client.get("/catalog", params={"merchant_id": "stale-1"}).json() == []

        raw = client.get("/catalog/raw", params={"merchant_id": "stale-1"}).json()
        assert raw["row_count"] == 1
        assert raw["rows"][0]["heading"] == "Something"

    def test_422_reports_what_was_stored(self):
        response = upload(b"reference,heading\nR1,Something\n", "bad.csv", "stale-2")
        assert response.status_code == 422
        body = response.json()
        assert body["products"] == []
        # A 422 is a warning, not a dead end: the agent can still sell from this sheet.
        assert body["raw"]["row_count"] == 1

    def test_replacing_a_catalog_replaces_it_entirely(self):
        upload(b"id,title,price\nP1,Old,10.00\nP2,Older,20.00\n", "a.csv", "stale-3")
        upload(b"id,title,price\nP9,New,30.00\n", "b.csv", "stale-3")

        assert [p["id"] for p in client.get("/catalog", params={"merchant_id": "stale-3"}).json()] == ["P9"]
        raw = client.get("/catalog/raw", params={"merchant_id": "stale-3"}).json()
        assert [r["id"] for r in raw["rows"]] == ["P9"]
        assert raw["source_filename"] == "b.csv"


class TestRawMetadata:
    def test_metadata_describes_the_sheet_not_the_slice(self):
        """`ids` filters rows. It must not change what the sheet is said to be."""
        upload(b"id,title,price\nP1,One,1.00\nP2,Two,2.00\nP3,Three,3.00\n", "m.csv", "meta-1")

        whole = client.get("/catalog/raw", params={"merchant_id": "meta-1"}).json()
        one = client.get(
            "/catalog/raw", params={"merchant_id": "meta-1", "ids": "P2"}
        ).json()

        assert one["row_count"] == 1  # row_count IS the slice, per §1.1
        assert one["columns"] == whole["columns"]
        assert one["uploaded_at"] == whole["uploaded_at"]
        assert one["source_filename"] == whole["source_filename"]

    def test_columns_union_across_ragged_rows(self):
        upload(b"id,title,price,note\nP1,One,1.00,\nP2,Two,2.00,hello\n", "r.csv", "meta-2")
        raw = client.get("/catalog/raw", params={"merchant_id": "meta-2"}).json()
        assert raw["columns"] == ["id", "title", "price", "note"]


class TestStockWriteback:
    """How the shop's inventory follows a sale. See docs/CONTRACTS.md §1.5."""

    SHEET = b"sku,title,price,qty_on_hand\nS1,Widget,10.00,4\nS2,Gadget,20.00,1\n"

    def test_writes_the_named_column_into_the_raw_row(self):
        upload(self.SHEET, "s.csv", "stock-1")

        response = client.post(
            "/catalog/stock-1/stock", json={"updates": {"S1": {"qty_on_hand": "3"}}}
        )
        assert response.status_code == 200
        assert response.json()["updated"] == 1

        rows = client.get("/catalog/raw", params={"merchant_id": "stock-1"}).json()["rows"]
        by_id = {r["sku"]: r for r in rows}
        assert by_id["S1"]["qty_on_hand"] == "3"
        assert by_id["S2"]["qty_on_hand"] == "1"  # untouched

    def test_the_caller_names_the_column(self):
        """The merchant does not know which column means stock, and must not guess.

        Whatever column it is handed is the column it writes.
        """
        upload(b"sku,title,price,widgets_left\nS1,Widget,10.00,7\n", "w.csv", "stock-2")
        client.post(
            "/catalog/stock-2/stock", json={"updates": {"S1": {"widgets_left": "6"}}}
        )
        rows = client.get("/catalog/raw", params={"merchant_id": "stock-2"}).json()["rows"]
        assert rows[0]["widgets_left"] == "6"

    def test_a_write_is_not_a_new_upload(self):
        """uploaded_at must not move: the console reads it as "when did my sheet change"."""
        upload(self.SHEET, "s.csv", "stock-3")
        before = client.get("/catalog/raw", params={"merchant_id": "stock-3"}).json()

        client.post("/catalog/stock-3/stock", json={"updates": {"S1": {"qty_on_hand": "0"}}})
        after = client.get("/catalog/raw", params={"merchant_id": "stock-3"}).json()

        assert after["uploaded_at"] == before["uploaded_at"]
        assert after["source_filename"] == before["source_filename"]
        assert after["id_column"] == before["id_column"]

    def test_unknown_rows_are_skipped_not_fatal(self):
        upload(self.SHEET, "s.csv", "stock-4")
        response = client.post(
            "/catalog/stock-4/stock",
            json={"updates": {"S1": {"qty_on_hand": "2"}, "NOPE": {"qty_on_hand": "9"}}},
        )
        assert response.status_code == 200
        assert response.json()["updated"] == 1

    def test_unknown_merchant_is_a_404(self):
        response = client.post(
            "/catalog/nobody-here/stock", json={"updates": {"X": {"qty": "1"}}}
        )
        assert response.status_code == 404

    def test_normalized_view_follows_the_raw_write(self):
        """The console's two reads must not disagree about the same product."""
        upload(self.SHEET, "s.csv", "stock-5")
        assert {p["id"]: p["stock"] for p in
                client.get("/catalog", params={"merchant_id": "stock-5"}).json()}["S1"] == 4

        client.post("/catalog/stock-5/stock", json={"updates": {"S1": {"qty_on_hand": "1"}}})

        products = {p["id"]: p for p in
                    client.get("/catalog", params={"merchant_id": "stock-5"}).json()}
        assert products["S1"]["stock"] == 1

    @pytest.mark.parametrize("value", ["sold out", "", None])
    def test_unparseable_stock_leaves_the_normalized_view_alone(self, value):
        """"In stock" and "sold out" are legitimate values in a merchant's own sheet.

        A cell we cannot read is one we must not translate into a 0 nobody asked for.
        """
        upload(self.SHEET, "s.csv", "stock-6")
        client.post("/catalog/stock-6/stock", json={"updates": {"S1": {"qty_on_hand": value}}})

        products = {p["id"]: p for p in
                    client.get("/catalog", params={"merchant_id": "stock-6"}).json()}
        assert products["S1"]["stock"] == 4  # unchanged
