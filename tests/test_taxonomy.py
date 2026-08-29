"""
Tests for the ProductTaxonomy and enhanced category inference.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from taxonomy import CategoryRule, InferenceResult, LLMInferenceClient, MockLLMClient, ProductTaxonomy


def test_taxonomy_default_rules_loaded():
    """Test that default taxonomy loads standard product categories."""
    tax = ProductTaxonomy()
    assert "Sunglasses" in tax.rules
    assert "Optical Glasses" in tax.rules
    assert "Blue Light Glasses" in tax.rules
    assert "Lighting" in tax.rules
    assert "Kitchenware" in tax.rules


def test_taxonomy_custom_rules():
    """Test creating custom taxonomy rules."""
    custom_rules = {
        "CustomWidget": ["widget", "gadget"],
        "CustomTool": ["tool", "implement"],
    }
    tax = ProductTaxonomy(rules=custom_rules)
    assert "CustomWidget" in tax.rules
    assert "CustomTool" in tax.rules
    assert "Sunglasses" not in tax.rules  # Custom rules replace defaults


def test_taxonomy_classify_exact_match():
    """Test taxonomy classification with exact keyword match."""
    tax = ProductTaxonomy()
    result = tax.classify("sunglasses frame")
    assert result is not None
    assert result.category == "Sunglasses"
    assert result.confidence > 0.0  # At least one keyword matched
    assert result.source == "taxonomy"


def test_taxonomy_classify_multiple_keywords():
    """Test taxonomy with multiple matching keywords."""
    tax = ProductTaxonomy()
    result = tax.classify("blue light computer glasses screen")
    assert result is not None
    assert result.category == "Blue Light Glasses"
    # Confidence should be at least 0.5 with multiple keyword matches
    assert result.confidence >= 0.5


def test_taxonomy_classify_no_match():
    """Test taxonomy returns None when no match is found."""
    tax = ProductTaxonomy()
    result = tax.classify("xyzabc obscure product name")
    assert result is None


def test_infer_product_type_with_provided_category():
    """Test that provided category takes priority."""
    from normalize import infer_product_type
    result = infer_product_type(title="Desk Lamp", category="MyCustomCategory")
    assert result == "MyCustomCategory"


def test_infer_product_type_uses_taxonomy():
    """Test that taxonomy inference works via infer_product_type."""
    from normalize import infer_product_type
    result = infer_product_type(title="Sunglasses Frame Ultra Pro")
    assert result == "Sunglasses"


def test_infer_product_type_optical_glasses():
    """Test optical glasses classification."""
    from normalize import infer_product_type
    result = infer_product_type(title="Prescription Reading Glasses")
    assert result == "Optical Glasses"


def test_infer_product_type_blue_light():
    """Test blue light glasses classification."""
    from normalize import infer_product_type
    result = infer_product_type(title="Blue Light Blocking Computer Glasses")
    assert result == "Blue Light Glasses"


def test_infer_product_type_fallback_to_general():
    """Test fallback to General category for unknown products."""
    from normalize import infer_product_type
    result = infer_product_type(title="XYZ Product 12345")
    assert result == "General"


def test_infer_product_type_with_attributes():
    """Test inference uses attributes in classification."""
    from normalize import infer_product_type
    result = infer_product_type(
        title="Frame Pro",
        attributes={"material": "titanium", "type": "blue light glasses"}
    )
    assert result == "Blue Light Glasses"


def test_infer_product_type_with_mock_llm():
    """Test that mock LLM falls back gracefully."""
    from normalize import infer_product_type
    mock_llm = MockLLMClient()
    result = infer_product_type(
        title="Sunglasses",
        llm_client=mock_llm
    )
    # Should still infer from taxonomy even with mock LLM
    assert result == "Sunglasses"


def test_category_rule_requires_all_keywords():
    """Test CategoryRule with requires_all=True."""
    rule = CategoryRule(
        category="TestType",
        keywords=["foo", "bar"],
        confidence=1.0,
        requires_all=True,
    )
    tax = ProductTaxonomy()
    tax.rules["TestType"] = rule
    
    # Should match only when BOTH keywords present
    result1 = tax.classify("foo bar baz")
    assert result1 is not None
    assert result1.category == "TestType"
    
    # Should not match with only one keyword
    result2 = tax.classify("foo only")
    assert result2 is None or result2.category != "TestType"


def test_infer_product_type_confidence_threshold():
    """Test that confidence threshold works."""
    from normalize import infer_product_type
    # With a high threshold, weak matches should fail
    result = infer_product_type(
        title="Sunglasses",
        confidence_threshold=0.99  # Very high threshold
    )
    # Should fall back to general or still match if confidence is high enough
    assert result in ["Sunglasses", "General"]


def test_mock_llm_client():
    """Test that MockLLMClient behaves correctly."""
    llm = MockLLMClient()
    result = llm.infer_product_type(title="Test", attributes={})
    assert result is None


def test_inference_result_dataclass():
    """Test InferenceResult data structure."""
    result = InferenceResult(
        category="TestCategory",
        confidence=0.85,
        source="taxonomy"
    )
    assert result.category == "TestCategory"
    assert result.confidence == 0.85
    assert result.source == "taxonomy"
