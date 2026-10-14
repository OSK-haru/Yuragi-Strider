"""Content-Security-Policy for API JSON, the desktop UI and the hosted worker."""

import asyncio
from pathlib import Path
import re
import unittest

from api.main import (API_CONTENT_SECURITY_POLICY, LOCAL_UI_CONTENT_SECURITY_POLICY,
                      PYTHON_RUNTIME_CONTENT_SECURITY_POLICY, app)

WORKER = Path(__file__).resolve().parents[1] / "frontend" / "src" / "worker.ts"


async def _headers(path):
    messages = []

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        messages.append(message)

    await app({"type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "method": "GET",
               "scheme": "http", "path": path, "raw_path": path.encode(), "query_string": b"",
               "headers": [(b"host", b"127.0.0.1")], "client": ("testclient", 1),
               "server": ("127.0.0.1", 80)}, receive, send)
    start = next(m for m in messages if m["type"] == "http.response.start")
    return {k.decode().lower(): v.decode() for k, v in start["headers"]}


def directives(policy):
    return {part.split()[0]: tuple(part.split()[1:]) for part in policy.split("; ")}


class SecurityHeaderTests(unittest.TestCase):
    def test_api_json_cannot_load_or_run_anything(self):
        headers = asyncio.run(_headers("/api/health"))
        self.assertEqual(headers["content-security-policy"], API_CONTENT_SECURITY_POLICY)
        self.assertEqual(directives(API_CONTENT_SECURITY_POLICY)["default-src"], ("'none'",))

    def test_non_api_paths_get_the_local_ui_policy(self):
        # The desktop launcher serves the SPA from these paths on the same app.
        for path in ("/", "/attack-simulator", "/assets/index.js"):
            self.assertEqual(asyncio.run(_headers(path))["content-security-policy"],
                             LOCAL_UI_CONTENT_SECURITY_POLICY)

    def test_only_the_python_runtime_gets_the_script_runner_policy(self):
        headers = asyncio.run(_headers("/assets/python-runtime/pythonWorker-abc.js"))
        self.assertEqual(headers["content-security-policy"], PYTHON_RUNTIME_CONTENT_SECURITY_POLICY)
        policy = directives(PYTHON_RUNTIME_CONTENT_SECURITY_POLICY)
        self.assertEqual(policy["connect-src"], ("'self'", "https://cdn.jsdelivr.net"))
        self.assertIn("'wasm-unsafe-eval'", policy["script-src"])
        self.assertNotIn("frame-ancestors", policy)
        # The page that hosts the Worker keeps the strict policy.
        self.assertNotIn("'unsafe-eval'", directives(LOCAL_UI_CONTENT_SECURITY_POLICY)["script-src"])

    def test_ui_is_cross_origin_isolated_and_api_json_is_not_changed(self):
        for path in ("/", "/assets/index.js", "/assets/python-runtime/pyodide.asm.wasm"):
            headers = asyncio.run(_headers(path))
            self.assertEqual(headers["cross-origin-opener-policy"], "same-origin")
            self.assertEqual(headers["cross-origin-embedder-policy"], "require-corp")
        self.assertNotIn("cross-origin-embedder-policy", asyncio.run(_headers("/api/health")))

    def test_developer_docs_keep_their_previous_behaviour(self):
        for path in ("/docs", "/redoc", "/openapi.json"):
            self.assertNotIn("content-security-policy", asyncio.run(_headers(path)))

    def test_local_ui_policy_blocks_inline_and_external_scripts_and_exfiltration(self):
        policy = directives(LOCAL_UI_CONTENT_SECURITY_POLICY)
        self.assertEqual(policy["script-src"], ("'self'",))
        self.assertEqual(policy["connect-src"], ("'self'",))
        for name in ("object-src", "base-uri", "frame-ancestors"):
            self.assertEqual(policy[name], ("'none'",))
        self.assertEqual(policy["form-action"], ("'self'",))

    def test_local_policy_matches_the_hosted_worker_except_the_hosted_api_origin(self):
        source = WORKER.read_text(encoding="utf-8")
        block = re.search(r"CONTENT_SECURITY_POLICY = \[(.*?)\]\.join", source, re.S).group(1)
        hosted = directives("; ".join(re.findall(r'"([^"]+)"', block)))
        local = directives(LOCAL_UI_CONTENT_SECURITY_POLICY)
        self.assertEqual(set(hosted), set(local))
        for name in hosted:
            if name == "connect-src":
                self.assertEqual(local[name], ("'self'",))
                self.assertIn("'self'", hosted[name])
            else:
                self.assertEqual(hosted[name], local[name], name)


if __name__ == "__main__":
    unittest.main()
