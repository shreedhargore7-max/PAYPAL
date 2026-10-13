"""Unit tests for robust parsing of OpenRouter model JSON responses."""
import pytest
from app.agent import _extract_json_object


def test_plain_json_is_parsed():
    result = _extract_json_object('{"issue_type":"ITEM_NOT_RECEIVED","confidence":0.9}')
    assert result["issue_type"] == "ITEM_NOT_RECEIVED"
    assert result["confidence"] == 0.9


def test_markdown_fenced_json_is_parsed():
    result = _extract_json_object('```json\n{"issue_type":"OTHER","confidence":0.4}\n```')
    assert result["issue_type"] == "OTHER"


def test_json_surrounded_by_explanation_is_extracted():
    text = 'Here is the result:\n{"issue_type":"ITEM_NOT_RECEIVED","summary":"Tracking says {delayed}","confidence":0.8}\nDone.'
    result = _extract_json_object(text)
    assert result["issue_type"] == "ITEM_NOT_RECEIVED"
    assert result["summary"] == "Tracking says {delayed}"


def test_empty_or_invalid_content_is_rejected():
    with pytest.raises(ValueError):
        _extract_json_object("")
    with pytest.raises(ValueError):
        _extract_json_object("No structured reply arrived")
    with pytest.raises(ValueError):
        _extract_json_object('{"issue_type":,}')
