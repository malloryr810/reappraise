# Uses the Vision REST API directly with a plain API key rather than the
# google-cloud-vision Python library — simpler setup, no service account required.

import base64
import os
from dataclasses import dataclass

import httpx

_ANNOTATE_URL = "https://vision.googleapis.com/v1/images:annotate"


@dataclass
class ItemLabel:
    name: str
    confidence: float


class VisionClient:
    def __init__(self) -> None:
        self._api_key = os.getenv("GOOGLE_VISION_API_KEY", "")

    def identify_item(self, image_bytes: bytes) -> list[ItemLabel]:
        response = httpx.post(
            _ANNOTATE_URL,
            # Header rather than ?key= so the key never appears in the URL —
            # httpx includes the full URL in HTTPStatusError messages
            headers={"X-Goog-Api-Key": self._api_key},
            json={
                "requests": [{
                    "image": {"content": base64.b64encode(image_bytes).decode()},
                    "features": [
                        {"type": "OBJECT_LOCALIZATION", "maxResults": 10},
                        {"type": "LABEL_DETECTION", "maxResults": 10},
                    ],
                }]
            },
            timeout=10,
        )
        response.raise_for_status()
        result = response.json()["responses"][0]

        seen: dict[str, float] = {}

        # Object localization names the specific item ("Digital Camera" vs. "Photography").
        # Insert its results first so its score wins when both detectors name the same thing.
        for obj in result.get("localizedObjectAnnotations", []):
            seen[obj["name"].lower()] = obj["score"]

        for label in result.get("labelAnnotations", []):
            key = label["description"].lower()
            if key not in seen:
                seen[key] = label["score"]

        return [
            ItemLabel(name=name, confidence=round(score, 4))
            for name, score in sorted(seen.items(), key=lambda x: -x[1])
        ]
