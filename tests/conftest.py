from __future__ import annotations

import json
from collections.abc import Callable

import httpx
import pytest

from flywire_checkout import FlywireClient, FlywireConfig

SANDBOX_BASE = "https://api-platform-sandbox.flywire.com/payments/v1/"
SESSION_ID = "494d2e9d-c0c9-407c-9094-5b3b2a02c00f"


@pytest.fixture
def config() -> FlywireConfig:
    return FlywireConfig(api_key="test-api-key", shared_secret="test-shared-secret", recipient_id="TQQ", max_retries=0)


class Recorder:
    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.handler: Callable[[httpx.Request], httpx.Response] = lambda r: httpx.Response(500)

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return self.handler(request)

    def json_body(self, index: int = -1) -> dict:
        return json.loads(self.requests[index].content)


@pytest.fixture
def recorder() -> Recorder:
    return Recorder()


@pytest.fixture
def client(config: FlywireConfig, recorder: Recorder) -> FlywireClient:
    return FlywireClient(config, http_client=httpx.Client(transport=httpx.MockTransport(recorder)))
