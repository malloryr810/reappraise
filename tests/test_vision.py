from unittest.mock import MagicMock, patch

from vision import ItemLabel, VisionClient


def _make_response(labels=None, objects=None):
    result = {}
    if labels:
        result["labelAnnotations"] = [
            {"description": desc, "score": score} for desc, score in labels
        ]
    if objects:
        result["localizedObjectAnnotations"] = [
            {"name": name, "score": score} for name, score in objects
        ]
    mock_resp = MagicMock()
    mock_resp.json.return_value = {"responses": [result]}
    return mock_resp


@patch("httpx.post")
def test_labels_sorted_by_confidence(mock_post):
    mock_post.return_value = _make_response(
        labels=[("Furniture", 0.80), ("Chair", 0.95)],
    )
    results = VisionClient().identify_item(b"fake")
    assert results[0].name == "chair"
    assert results[0].confidence == 0.95


@patch("httpx.post")
def test_object_localization_takes_priority_over_label(mock_post):
    mock_post.return_value = _make_response(
        labels=[("camera", 0.90)],
        objects=[("Digital Camera", 0.97)],
    )
    results = VisionClient().identify_item(b"fake")
    assert results[0].name == "digital camera"
    assert results[0].confidence == 0.97


@patch("httpx.post")
def test_deduplicates_same_name_keeping_object_score(mock_post):
    mock_post.return_value = _make_response(
        labels=[("camera", 0.88)],
        objects=[("camera", 0.95)],
    )
    results = VisionClient().identify_item(b"fake")
    camera_hits = [r for r in results if r.name == "camera"]
    assert len(camera_hits) == 1
    assert camera_hits[0].confidence == 0.95


@patch("httpx.post")
def test_empty_response_returns_empty_list(mock_post):
    mock_post.return_value = _make_response()
    assert VisionClient().identify_item(b"fake") == []


@patch("httpx.post")
def test_returns_item_labels(mock_post):
    mock_post.return_value = _make_response(labels=[("Watch", 0.91)])
    results = VisionClient().identify_item(b"fake")
    assert all(isinstance(r, ItemLabel) for r in results)


@patch("httpx.post")
def test_api_key_sent_in_header_not_url(mock_post, monkeypatch):
    monkeypatch.setenv("GOOGLE_VISION_API_KEY", "secret-key")
    mock_post.return_value = _make_response()

    VisionClient().identify_item(b"img")

    _, kwargs = mock_post.call_args
    assert kwargs["headers"] == {"X-Goog-Api-Key": "secret-key"}
    assert "params" not in kwargs
