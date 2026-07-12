import base64
import json
import io
import tempfile
import unittest
import zipfile
from pathlib import Path

import cpa_sub2api_export as export


def fake_access_token(**payload):
    def encode(data):
        raw = json.dumps(data, separators=(",", ":")).encode("utf-8")
        return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")

    body = {
        "sub": "sub-123",
        "scope": "openid profile email offline_access grok-cli:access api:access",
        "referrer": "grok-build",
        **payload,
    }
    return f"{encode({'alg': 'none', 'typ': 'JWT'})}.{encode(body)}."


def sample_cpa(email="user@example.com"):
    return {
        "type": "xai",
        "auth_kind": "oauth",
        "access_token": fake_access_token(),
        "refresh_token": "refresh-token",
        "id_token": "id-token",
        "token_type": "Bearer",
        "expires_in": 21600,
        "expired": "2026-07-11T08:58:12Z",
        "last_refresh": "2026-07-11T02:58:11Z",
        "email": email,
        "sub": "sub-123",
        "base_url": "https://cli-chat-proxy.grok.com/v1",
        "token_endpoint": "https://auth.x.ai/oauth2/token",
        "redirect_uri": "http://127.0.0.1:56121/callback",
        "disabled": False,
        "headers": {"x-grok-client-version": "0.2.93"},
    }


class CpaSub2ApiExportTests(unittest.TestCase):
    def test_build_sub2api_document_from_cpa_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cpa = sample_cpa()
            (root / "xai-user.json").write_text(json.dumps(cpa), encoding="utf-8")

            document, summary = export.build_sub2api_document(root)

        self.assertEqual(summary["count"], 1)
        self.assertEqual(document["proxies"], [])
        self.assertEqual(len(document["accounts"]), 1)
        account = document["accounts"][0]
        self.assertEqual(account["platform"], "openai")
        self.assertEqual(account["type"], "oauth")
        self.assertEqual(account["concurrency"], 10)
        self.assertEqual(account["priority"], 1)
        self.assertEqual(account["credentials"]["access_token"], cpa["access_token"])
        self.assertEqual(account["credentials"]["refresh_token"], "refresh-token")
        self.assertEqual(account["credentials"]["base_url"], "https://cli-chat-proxy.grok.com/v1")
        self.assertEqual(account["extra"]["email_key"], "user_example_com")

    def test_build_cpa_zip_contains_files_and_manifest(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "xai-user.json").write_text(json.dumps(sample_cpa()), encoding="utf-8")

            payload, summary = export.build_cpa_zip(root)

        self.assertEqual(summary["count"], 1)
        with zipfile.ZipFile(io.BytesIO(payload)) as zf:
            self.assertIn("xai-user.json", zf.namelist())
            self.assertIn("manifest.json", zf.namelist())
            manifest = json.loads(zf.read("manifest.json").decode("utf-8"))
            self.assertEqual(manifest["count"], 1)


if __name__ == "__main__":
    unittest.main()
