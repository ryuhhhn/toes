<<<<<<< HEAD
# React + Vite

This template provides a minimal setup to get React working in Vite with HMR and some Oxlint rules.

Currently, two official plugins are available:
- [@vitejs/plugin-react-swc](https://github.com/vitejs/vite-plugin-react/blob/main/packages/plugin-react-swc) uses [SWC](https://swc.rs/)

## React Compiler

The React Compiler is not enabled on this template because of its impact on dev & build performances. To add it, see [this documentation](https://react.dev/learn/react-compiler/installation).

## Merchant Backend API

This repository also contains a FastAPI merchant catalog service for CSV ingestion, normalization, storage, and merchant configuration.

### API setup
```bash
pip install -r requirements.txt
python3 -m uvicorn app.main:app --reload --port 8000
```

Open the interactive API documentation at `http://127.0.0.1:8000/docs`.

### API endpoints
- `POST /catalog/upload` - Upload a merchant CSV.
- `GET /catalog?merchant_id=` - List a merchant's products.
- `GET /catalog/search?merchant_id=&query=&category=&min_price=&max_price=&in_stock_only=` - Search products.
- `PATCH /catalog/{product_id}?merchant_id=` - Update a product.

### Product normalization
Required fields are `id`, `title`, and `price`. Missing stock defaults to `1` with a warning; missing images produce a warning without blocking the upload. Categories use provided values, optional LLM inference, configurable taxonomy matching, and finally `General`.

Run the backend tests with:
```bash
pytest -q
```
print(report.to_dict())
"
```
This exercises the real logic without needing FastAPI installed.
>>>>>>> 7ac3c08 (Complete catalog API and LLM integration)
