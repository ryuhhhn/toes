#!/usr/bin/env python3
"""
Example: Using OpenAI LLM for product category inference.

This script demonstrates:
1. Basic LLM inference
2. Fallback to taxonomy when LLM fails
3. CSV processing with LLM
4. Integration with the API
"""
import io
import sys
from pathlib import Path

# Add parent directory to path
sys.path.insert(0, str(Path(__file__).resolve().parents[0]))

import pandas as pd

def example_1_basic_llm_inference():
    """Example 1: Basic inference with LLM (requires OPENAI_API_KEY)."""
    print("\n" + "="*60)
    print("EXAMPLE 1: Basic LLM Inference")
    print("="*60)
    
    try:
        from llm_client import OpenAIInferenceClient
        from normalize import infer_product_type
        
        # Create LLM client - will fail if OPENAI_API_KEY not set
        print("Initializing OpenAI client...")
        llm = OpenAIInferenceClient()
        
        # Test inference
        test_cases = [
            "Oakley Pro Sunglasses",
            "Blue Light Blocking Glasses",
            "Desk Lamp with Adjustable Brightness",
            "Wireless Bluetooth Speaker",
        ]
        
        for title in test_cases:
            category = infer_product_type(title=title, llm_client=llm)
            print(f"  '{title}' → {category}")
            
    except ValueError as e:
        print(f"✗ {e}")
        print("  Set OPENAI_API_KEY to use this example")


def example_2_taxonomy_only():
    """Example 2: Using taxonomy only (no LLM needed)."""
    print("\n" + "="*60)
    print("EXAMPLE 2: Taxonomy-Only Inference (No API Key Needed)")
    print("="*60)
    
    from normalize import infer_product_type
    
    test_cases = [
        ("Oakley Sunglasses", "Sunglasses"),
        ("Blue Light Blocker", "Blue Light Glasses"),
        ("Desk Lamp", "Lighting"),
        ("Coffee Mug", "Kitchenware"),
        ("Leather Wallet", "Accessories"),
    ]
    
    print("Testing taxonomy-based inference:")
    for title, expected in test_cases:
        result = infer_product_type(title=title)
        status = "✓" if result == expected else "✗"
        print(f"  {status} '{title}' → {result} (expected: {expected})")


def example_3_csv_with_llm():
    """Example 3: Process CSV with LLM inference."""
    print("\n" + "="*60)
    print("EXAMPLE 3: CSV Processing with LLM")
    print("="*60)
    
    # Create sample CSV
    csv_content = """id,title,price,description
P1,Oakley Holbrook Frame,99.99,Premium sunglasses for outdoor use
P2,Blue Light Computer Glasses,45.00,Screen protection for office work
P3,Reading Glasses,35.00,Optical glasses for close-up viewing
P4,Desk Work Light,55.00,LED lamp for better workspace illumination"""
    
    try:
        from llm_client import OpenAIInferenceClient
        from app.normalize import normalize_csv
        
        print("Initializing LLM...")
        llm = OpenAIInferenceClient()
        
        print("Processing CSV with LLM inference:")
        df = pd.read_csv(io.StringIO(csv_content))
        products, report = normalize_csv(df, merchant_id="example", llm_client=llm)
        
        print(f"  ✓ Processed {report.rows_out} products")
        for p in products:
            print(f"    - {p['id']:3s}: {p['title']:30s} → {p['category']}")
            
    except ValueError:
        print("✗ OpenAI API key not available")
        example_3_csv_without_llm()


def example_3_csv_without_llm():
    """Example 3: Process CSV without LLM (fallback to taxonomy)."""
    print("\n" + "="*60)
    print("EXAMPLE 3b: CSV Processing with Taxonomy Only")
    print("="*60)
    
    # Create sample CSV
    csv_content = """id,title,price,description
P1,Oakley Holbrook Frame,99.99,Premium sunglasses for outdoor use
P2,Blue Light Computer Glasses,45.00,Screen protection for office work
P3,Reading Glasses,35.00,Optical glasses for close-up viewing
P4,Desk Work Light,55.00,LED lamp for better workspace illumination"""
    
    from app.normalize import normalize_csv
    
    print("Processing CSV with taxonomy inference (no API key needed):")
    df = pd.read_csv(io.StringIO(csv_content))
    products, report = normalize_csv(df, merchant_id="example")
    
    print(f"  ✓ Processed {report.rows_out} products")
    for p in products:
        print(f"    - {p['id']:3s}: {p['title']:30s} → {p['category']}")


def example_4_api_test():
    """Example 4: API upload with automatic category inference."""
    print("\n" + "="*60)
    print("EXAMPLE 4: API Upload with Category Inference")
    print("="*60)
    
    from fastapi.testclient import TestClient
    from app.main import app
    
    client = TestClient(app)
    
    csv_content = """id,title,price
S1,Sunglasses Pro,99.99
B1,Blue Light Blocker,45.00
L1,Table Lamp,35.00"""
    
    print("Uploading CSV via API...")
    files = {
        "file": ("products.csv", io.BytesIO(csv_content.encode())),
    }
    data = {"merchant_id": "example-merchant"}
    
    response = client.post("/catalog/upload", files=files, data=data)
    result = response.json()
    
    print(f"  Status: {response.status_code}")
    print(f"  Rows processed: {result['report']['rows_out']}")
    print("  Categories inferred:")
    for p in result["products"]:
        print(f"    - {p['id']:3s}: {p['title']:25s} → {p['category']}")
    
    # Test listing
    list_resp = client.get("/catalog?merchant_id=example-merchant")
    print(f"  ✓ Listed {len(list_resp.json())} products")


def example_5_custom_taxonomy():
    """Example 5: Using a custom product taxonomy."""
    print("\n" + "="*60)
    print("EXAMPLE 5: Custom Taxonomy")
    print("="*60)
    
    from taxonomy import ProductTaxonomy
    from normalize import infer_product_type
    
    # Create custom taxonomy for a different product category
    custom_rules = {
        "Sports Equipment": ["ball", "racket", "bat", "glove", "helmet"],
        "Board Games": ["board game", "card game", "puzzle", "dice"],
        "Toys": ["toy", "doll", "action figure", "lego"],
    }
    
    custom_taxonomy = ProductTaxonomy(rules=custom_rules)
    
    test_cases = [
        "Soccer Ball",
        "Wooden Chess Board",
        "Barbie Doll",
    ]
    
    print("Testing custom taxonomy:")
    for title in test_cases:
        result = infer_product_type(title=title, taxonomy=custom_taxonomy)
        print(f"  '{title}' → {result}")


if __name__ == "__main__":
    print("\n" + "🚀 "*15)
    print("Product Category Inference Examples")
    print("🚀 "*15)
    
    # Run examples
    example_2_taxonomy_only()
    example_1_basic_llm_inference()
    example_3_csv_with_llm()
    example_4_api_test()
    example_5_custom_taxonomy()
    
    print("\n" + "="*60)
    print("✓ All examples completed!")
    print("="*60 + "\n")
