"""Cross-language contract check for the first Rust HTTP route migration."""

import json
from pathlib import Path
import unittest

from api.main import app, health


CONTRACT = (
    Path(__file__).resolve().parents[1]
    / "rust_kernels"
    / "yuragi_strider_api"
    / "contracts"
    / "api-health-v1.json"
)


class ApiHealthContractTests(unittest.TestCase):
    def test_fastapi_health_response_matches_the_shared_wire_contract(self) -> None:
        expected = json.loads(CONTRACT.read_text(encoding="utf-8"))

        self.assertEqual(health(), expected)

    def test_fastapi_keeps_health_route_and_success_response(self) -> None:
        operation = app.openapi()["paths"]["/api/health"]["get"]

        self.assertEqual(operation["responses"]["200"]["description"], "Successful Response")
        self.assertIn("application/json", operation["responses"]["200"]["content"])


if __name__ == "__main__":
    unittest.main()
