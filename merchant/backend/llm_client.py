"""
OpenAI-based product category inference client.

Requires: OPENAI_API_KEY environment variable
Install: pip install openai
"""
from __future__ import annotations

import os
import json
from typing import Any

from taxonomy import InferenceResult, LLMInferenceClient


class OpenAIInferenceClient(LLMInferenceClient):
    """
    LLM-based product category inference using OpenAI's API.
    
    Uses GPT to infer product categories based on title and attributes.
    Falls back gracefully if API is unavailable or rate-limited.
    """

    def __init__(self, api_key: str | None = None, model: str = "gpt-3.5-turbo"):
        """
        Initialize OpenAI inference client.

        Args:
            api_key: OpenAI API key. Defaults to OPENAI_API_KEY env var.
            model: Model to use. Defaults to gpt-3.5-turbo (fast, cheaper).
                   Use gpt-4 for better accuracy if budget allows.
        """
        self.api_key = api_key or os.getenv("OPENAI_API_KEY")
        if not self.api_key:
            raise ValueError(
                "OpenAI API key not provided. Set OPENAI_API_KEY env var or pass api_key parameter."
            )
        self.model = model
        self._client = None

    @property
    def client(self):
        """Lazy-load OpenAI client."""
        if self._client is None:
            try:
                from openai import OpenAI
                self._client = OpenAI(api_key=self.api_key)
            except ImportError:
                raise ImportError(
                    "OpenAI package not installed. Install with: pip install openai"
                )
        return self._client

    def infer_product_type(
        self,
        title: str,
        attributes: dict[str, Any] | None = None,
    ) -> InferenceResult | None:
        """
        Infer product category using OpenAI GPT.

        Args:
            title: product title
            attributes: additional product attributes

        Returns:
            InferenceResult with LLM-predicted category, or None on failure.
        """
        try:
            # Build context from attributes
            attr_str = ""
            if attributes:
                attr_items = [f"{k}: {v}" for k, v in attributes.items() if v]
                attr_str = "\n".join(attr_items)

            # Construct the prompt
            prompt = f"""You are a product categorization expert. Categorize the following product.

Product Title: {title}
"""
            if attr_str:
                prompt += f"""Additional Attributes:
{attr_str}
"""

            prompt += """Based on this information, respond with ONLY a single product category name (e.g., "Sunglasses", "Electronics", "Furniture"). 
Be concise and choose from common retail categories. If unsure, respond "General"."""

            # Call OpenAI API
            response = self.client.chat.completions.create(
                model=self.model,
                messages=[
                    {
                        "role": "system",
                        "content": "You are a product categorization expert. Respond with only the category name.",
                    },
                    {"role": "user", "content": prompt},
                ],
                temperature=0.3,  # Low temperature for consistent results
                max_tokens=20,  # Category names are short
                timeout=5,  # 5 second timeout
            )

            category = response.choices[0].message.content.strip()
            if category:
                return InferenceResult(
                    category=category,
                    confidence=0.85,  # Trust GPT's inference
                    source="llm",
                )

        except Exception as e:
            # Log error but don't crash; fallback to taxonomy will handle it
            import warnings
            warnings.warn(f"OpenAI inference failed: {e}", stacklevel=2)

        return None

    def parse_search_request(self, message: str) -> dict[str, Any] | None:
        """Convert a shopper message into filters understood by the catalog API."""
        try:
            response = self.client.chat.completions.create(
                model=self.model,
                messages=[
                    {
                        "role": "system",
                        "content": (
                            "Extract catalog search filters from the shopper message. "
                            "Return JSON only with keys query, category, min_price, "
                            "max_price, in_stock_only, and attribute_filters. "
                            "attribute_filters must map any catalog field name to either "
                            "a text value or an object with min, max, or contains. "
                            "For example, 'weight less than 20g' becomes "
                            "{\"attribute_filters\": {\"weight\": {\"max\": 20}}}. "
                            "Use null for unknown values and true for in_stock_only unless "
                            "the shopper asks for unavailable items."
                        ),
                    },
                    {"role": "user", "content": message},
                ],
                temperature=0,
                max_tokens=120,
                response_format={"type": "json_object"},
                timeout=8,
            )
            payload = json.loads(response.choices[0].message.content or "{}")
            allowed = {
                "query", "category", "min_price", "max_price",
                "in_stock_only", "attribute_filters",
            }
            return {key: payload.get(key) for key in allowed if key in payload}
        except Exception as e:
            import warnings
            warnings.warn(f"OpenAI search parsing failed: {e}", stacklevel=2)
            return None
