"""Cross-language contract check for the Rust circuit preview slice."""

import json
from pathlib import Path
import unittest

from api.main import CompilePreviewRequest, app, compile_circuit_preview


CONTRACT = (
    Path(__file__).resolve().parents[1]
    / "rust_kernels"
    / "yuragi_strider_api"
    / "contracts"
    / "circuit-compile-bell-logical-direct-v1.json"
)


class ApiCircuitCompileContractTests(unittest.TestCase):
    def test_fastapi_bell_preview_matches_the_shared_rust_contract(self) -> None:
        expected = json.loads(CONTRACT.read_text(encoding="utf-8"))
        actual = compile_circuit_preview(CompilePreviewRequest(circuit_preset="bell"))

        self.assertEqual(actual, expected)

    def test_compile_route_retains_its_public_path_and_success_response(self) -> None:
        operation = app.openapi()["paths"]["/api/circuit/compile"]["post"]

        self.assertEqual(operation["responses"]["200"]["description"], "Successful Response")
        self.assertIn("application/json", operation["responses"]["200"]["content"])


if __name__ == "__main__":
    unittest.main()
