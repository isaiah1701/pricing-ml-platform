"""Verify that the approved alias is ready and produces a valid prediction."""

from __future__ import annotations

import json
import logging
import math
import os
from pathlib import Path
from urllib.parse import urlparse
from urllib.request import Request, urlopen

LOGGER = logging.getLogger(__name__)
PAYLOADS = Path(__file__).resolve().parents[1] / "tests/k6/listings.json"


def verify(url: str, expected_version: str = "") -> None:
    """Require ready champion identity and a finite prediction from the same model version."""
    parsed = urlparse(url)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.netloc
        or parsed.username
        or parsed.password
    ):
        raise ValueError("Use a serving URL without embedded credentials")
    endpoint = url.rstrip("/")
    with urlopen(f"{endpoint}/ready", timeout=20) as response:
        ready = json.load(response)
    if (
        ready.get("status") != "ready"
        or ready.get("alias") != "champion"
        or not ready.get("version")
    ):
        raise ValueError("Approved champion is not ready")
    if expected_version and ready["version"] != expected_version:
        raise ValueError(
            "Inference has not loaded the explicitly selected model version"
        )
    payload = json.loads(PAYLOADS.read_text())[0]
    request = Request(
        f"{endpoint}/predict",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urlopen(request, timeout=30) as response:
        prediction = json.load(response)
    price = prediction.get("predicted_price")
    if (
        type(price) not in {int, float}
        or not math.isfinite(price)
        or price < 0
        or prediction.get("model_version") != ready["version"]
        or prediction.get("model_alias") != "champion"
        or not prediction.get("prediction_id")
    ):
        LOGGER.error("Inference response did not match the ready approved model")
        raise ValueError("Approved-model inference verification failed")
    LOGGER.info(
        "Verified champion readiness and inference at model version %s",
        ready["version"],
    )


def main() -> None:
    """Run the lightweight post-deployment verification without retraining."""
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    verify(os.environ["SERVING_URL"], os.environ.get("EXPECTED_MODEL_VERSION", ""))


if __name__ == "__main__":
    main()
