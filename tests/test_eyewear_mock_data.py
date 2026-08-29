import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from normalize import normalize_csv


def test_eyewear_csv_uploads_and_preserves_known_categories():
    df = pd.read_csv(Path(__file__).resolve().parents[1] / 'merchant' / 'backend' / 'eyewear_mock_data.csv')

    products, report = normalize_csv(df, 'demo-eyewear')

    assert report.ok is True
    assert report.rows_in == 50
    assert report.rows_out == 50
    assert report.missing_required_columns == []
    assert any('Image URL missing' in warning for warning in report.warnings)

    first = next(p for p in products if p['id'] == 'EYE-1001')
    assert first['category'] == 'Sunglasses'

    optical = next(p for p in products if p['id'] == 'EYE-1005')
    assert optical['category'] == 'Optical Glasses'

    blue_light = next(p for p in products if p['id'] == 'EYE-1010')
    assert blue_light['category'] == 'Blue Light Glasses'
