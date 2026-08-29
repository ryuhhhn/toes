"""
Product category taxonomy and LLM inference abstraction.

Provides:
- ProductTaxonomy: configurable category rules with confidence scoring
- LLMInferenceClient: abstract base class for LLM-based category inference
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any


@dataclass
class CategoryRule:
    """A single category classification rule with confidence metadata."""
    category: str
    keywords: list[str]
    confidence: float = 1.0  # 0.0-1.0 score for keyword match confidence
    requires_all: bool = False  # if True, ALL keywords must match; else ANY match succeeds


@dataclass
class InferenceResult:
    """Result of a category inference with confidence and source."""
    category: str
    confidence: float  # 0.0-1.0
    source: str  # "provided", "taxonomy", "llm", "heuristic", or "default"


class ProductTaxonomy:
    """Configurable product category taxonomy with keyword rules and scoring."""

    def __init__(self, rules: dict[str, list[str]] | None = None):
        """
        Initialize taxonomy with category rules.

        Args:
            rules: dict mapping category names to lists of keywords.
                   If None, uses the default taxonomy.
        """
        self.rules: dict[str, CategoryRule] = {}
        if rules:
            for category, keywords in rules.items():
                self.add_rule(category, keywords)
        else:
            self._load_default_taxonomy()

    def _load_default_taxonomy(self) -> None:
        """Load the default product taxonomy."""
        default_rules = {
            "Sunglasses": ["sunglass", "sun glass", "shades", "sunshade"],
            "Optical Glasses": ["optical", "prescription", "reading", "vision", "eyeglasses"],
            "Blue Light Glasses": ["blue light", "computer glasses", "screen glasses", "night shift"],
            "Lighting": ["lamp", "light", "bulb", "lantern", "ceiling", "fixture"],
            "Kitchenware": ["mug", "cup", "plate", "bowl", "kettle", "pan", "cutlery", "jar"],
            "Accessories": ["wallet", "belt", "watch", "bag", "hat", "scarf", "gloves", "bracelet"],
            "Electronics": ["speaker", "charger", "headphone", "usb", "keyboard", "monitor", "tablet", "router"],
            "Furniture": ["chair", "desk", "table", "shelf", "cabinet", "sofa", "stool", "bench"],
            "Apparel": ["shirt", "jacket", "dress", "shoe", "sneaker", "jeans", "hoodie", "socks"],
            "Office": ["notebook", "pen", "desk", "chair", "printer", "monitor", "binder", "calendar"],
        }
        for category, keywords in default_rules.items():
            self.add_rule(category, keywords)

    def add_rule(
        self,
        category: str,
        keywords: list[str],
        confidence: float = 1.0,
        requires_all: bool = False,
    ) -> None:
        """Add or update a category classification rule."""
        self.rules[category] = CategoryRule(
            category=category,
            keywords=keywords,
            confidence=confidence,
            requires_all=requires_all,
        )

    def classify(self, haystack: str) -> InferenceResult | None:
        """
        Classify text against taxonomy rules.

        Args:
            haystack: lowercased text to search (title + attributes)

        Returns:
            InferenceResult with matched category and confidence, or None if no match.
        """
        best_result = None
        for rule in self.rules.values():
            matches = sum(1 for kw in rule.keywords if kw in haystack)
            if rule.requires_all and matches == len(rule.keywords):
                confidence = rule.confidence
            elif not rule.requires_all and matches > 0:
                # Fractional confidence based on keyword ratio
                confidence = rule.confidence * (matches / len(rule.keywords))
            else:
                continue

            if best_result is None or confidence > best_result.confidence:
                best_result = InferenceResult(
                    category=rule.category,
                    confidence=confidence,
                    source="taxonomy",
                )

        return best_result


class LLMInferenceClient(ABC):
    """Abstract base class for LLM-based product category inference."""

    @abstractmethod
    def infer_product_type(
        self,
        title: str,
        attributes: dict[str, Any] | None = None,
    ) -> InferenceResult | None:
        """
        Infer product category using LLM.

        Args:
            title: product title
            attributes: additional product attributes

        Returns:
            InferenceResult with LLM-predicted category, or None on failure.
        """
        pass


class MockLLMClient(LLMInferenceClient):
    """Mock LLM client for testing without a real external service."""

    def infer_product_type(
        self,
        title: str,
        attributes: dict[str, Any] | None = None,
    ) -> InferenceResult | None:
        """Mock implementation that returns None (falls back to taxonomy)."""
        return None
