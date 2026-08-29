# OpenAI LLM Integration & API Testing Guide

## Quick Start

### 1. Set Up OpenAI API Key

Get your API key from [OpenAI Dashboard](https://platform.openai.com/account/api-keys).

**Option A: Environment Variable (Recommended)**
```bash
export OPENAI_API_KEY="sk-..."
```

**Option B: Pass Directly in Code**
```python
from llm_client import OpenAIInferenceClient
llm = OpenAIInferenceClient(api_key="sk-...")
```

### 2. Install Dependencies

```bash
pip install -r requirements.txt
```

### 3. Using LLM in Your Code

**Simple usage:**
```python
from llm_client import OpenAIInferenceClient
from normalize import infer_product_type

# Create LLM client
llm = OpenAIInferenceClient()

# Use in inference
category = infer_product_type(
    title="Oakley Sunglasses",
    llm_client=llm
)
print(category)  # Sunglasses
```

**In CSV normalization:**
```python
import pandas as pd
from app.normalize import normalize_csv
from llm_client import OpenAIInferenceClient

df = pd.read_csv('products.csv')
llm = OpenAIInferenceClient()

# Pass LLM to normalization
products, report = normalize_csv(
    df,
    merchant_id='my-merchant',
    llm_client=llm
)
```

**In FastAPI app:**
```python
# Modify app/main.py to initialize LLM at startup
from llm_client import OpenAIInferenceClient

llm_client = None

@app.on_event("startup")
async def startup_event():
    global llm_client
    try:
        llm_client = OpenAIInferenceClient()
        print("✓ OpenAI LLM loaded")
    except Exception as e:
        print(f"✗ LLM initialization failed: {e}")
        llm_client = None  # Fall back to taxonomy-only
```

---

## API Testing

### Run All Tests

```bash
pytest tests/ -v
```

### Run Specific Test Suites

```bash
# API tests only
pytest tests/test_api.py -v

# Taxonomy tests
pytest tests/test_taxonomy.py -v

# Inference tests
pytest tests/test_product_inference.py -v

# Eyewear mock data validation
pytest tests/test_eyewear_mock_data.py -v
```

### Run Specific Test

```bash
pytest tests/test_api.py::TestUploadWorkflows::test_upload_with_categories -v
```

### Run with Coverage

```bash
pytest tests/ --cov=. --cov-report=html
# Open htmlcov/index.html
```

---

## What Each Test Suite Covers

### `test_api.py` - Comprehensive API Tests

**TestHealthcheck**
- ✓ Health endpoint responds

**TestUploadWorkflows**
- ✓ Basic CSV upload
- ✓ Category inference
- ✓ Missing stock handling
- ✓ Missing image URL handling
- ✓ Header aliasing
- ✓ Missing required fields
- ✓ Extra columns in attributes
- ✓ Invalid price handling
- ✓ Large dataset upload (100 rows)

**TestListOperations**
- ✓ List empty merchant
- ✓ List after upload

**TestSearchOperations**
- ✓ Search by query/keyword
- ✓ Search by category
- ✓ Search by price range
- ✓ Search in-stock only
- ✓ Combined filters

**TestUpdateOperations**
- ✓ Patch product title
- ✓ Patch product price
- ✓ Patch product stock
- ✓ Patch nonexistent product (404)

**TestErrorHandling**
- ✓ Empty CSV
- ✓ Malformed CSV
- ✓ Missing merchant_id
- ✓ Search without merchant_id

### `test_taxonomy.py` - Category Inference Tests
- ✓ Default taxonomy loading
- ✓ Custom taxonomy rules
- ✓ Keyword matching
- ✓ Confidence scoring
- ✓ LLM integration
- ✓ Fallback behavior

### `test_product_inference.py` - Basic Inference Tests
- ✓ Generic product categories

### `test_eyewear_mock_data.py` - Real Dataset Validation
- ✓ Eyewear CSV processing
- ✓ Category preservation

---

## Performance Considerations

### LLM Costs
- **gpt-3.5-turbo**: ~$0.0005 per category inference (fast, cheap)
- **gpt-4**: ~$0.01 per category inference (slower, more accurate)

### Optimization Tips
1. **Cache results**: Don't call LLM for same title twice
2. **Fallback to taxonomy first**: Let keyword matching handle obvious cases
3. **Confidence threshold**: Only use LLM when confidence < 0.5
4. **Batch requests**: For bulk uploads, consider batch processing

### Example: Caching

```python
from functools import lru_cache

class CachedOpenAIClient(OpenAIInferenceClient):
    @lru_cache(maxsize=1000)
    def infer_product_type(self, title: str, **kwargs):
        return super().infer_product_type(title, **kwargs)
```

---

## Troubleshooting

### "OpenAI API key not provided"
```bash
# Check env var is set
echo $OPENAI_API_KEY

# If not set:
export OPENAI_API_KEY="sk-..."
```

### "OpenAI package not installed"
```bash
pip install openai
```

### Rate Limiting (429 errors)
- OpenAI has rate limits per plan
- Add exponential backoff in production
- Consider async/concurrent inference

### High Latency
- Use gpt-3.5-turbo instead of gpt-4
- Add timeout (currently 5 seconds)
- Cache frequent queries
- Use mock client for testing

### Test Failures

Check that:
1. ✓ Requirements installed: `pip install -r requirements.txt`
2. ✓ OPENAI_API_KEY set (for LLM tests, can be skipped)
3. ✓ Database accessible (for storage tests)
4. ✓ No port conflicts (if running server)

---

## Example: End-to-End API Test

```bash
# Terminal 1: Start server
source .venv/bin/activate
export OPENAI_API_KEY="sk-..."
python3 -m uvicorn app.main:app --port 8000

# Terminal 2: Run tests
source .venv/bin/activate
pytest tests/test_api.py -v
```

---

## Next Steps

1. **Implement caching** to reduce API costs
2. **Add async inference** for bulk uploads
3. **Monitor costs** and adjust model/frequency
4. **Tune confidence threshold** based on real data
5. **Add logging** for LLM calls in production

For questions or issues, check the taxonomy and llm_client source code!
