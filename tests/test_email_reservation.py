import tempfile
import unittest
from pathlib import Path
from unittest import mock

import outlook_mail


class EmailReservationTests(unittest.TestCase):
    def test_outlook_acquire_marks_reserved_and_never_reuses(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            pool = root / "outlook_mailboxes.txt"
            state = root / "outlook_state.json"
            pool.write_text(
                "user@example.com----pass----client----refresh\n",
                encoding="utf-8",
            )

            mailbox = outlook_mail.acquire_mailbox(
                pool,
                state,
                {"outlook_alias_count": 0, "outlook_alias_include_original": True},
            )

            self.assertEqual(mailbox["address"], "user@example.com")
            stats = outlook_mail.pool_stats(
                pool,
                state,
                {"outlook_alias_count": 0, "outlook_alias_include_original": True},
            )
            self.assertEqual(stats["reserved"], 1)
            self.assertEqual(stats["available"], 0)
            with self.assertRaises(RuntimeError):
                outlook_mail.acquire_mailbox(
                    pool,
                    state,
                    {"outlook_alias_count": 0, "outlook_alias_include_original": True},
                )

    def test_outlook_token_failure_stops_polling(self):
        mailbox = {
            "address": "user@example.com",
            "login_email": "user@example.com",
            "client_id": "client",
            "refresh_token": "refresh",
            "mode": "auto",
        }
        calls = []

        def fail_refresh(client_id, refresh_token, scope, user_agent, proxy=""):
            calls.append(scope)
            raise outlook_mail.OutlookTokenError("token_refresh_failed_http_400: AADSTS70000")

        with mock.patch.object(outlook_mail, "_exchange_refresh_token", side_effect=fail_refresh):
            with self.assertRaises(outlook_mail.OutlookTokenError):
                outlook_mail.wait_for_code(
                    mailbox,
                    extract_code=lambda text, subject: None,
                    timeout=30,
                    poll_interval=0.01,
                )

        self.assertEqual(calls, [outlook_mail.OUTLOOK_GRAPH_SCOPE, outlook_mail.OUTLOOK_IMAP_SCOPE])

    def test_outlook_token_invalid_marks_parent_unavailable(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            pool = root / "outlook_mailboxes.txt"
            state = root / "outlook_state.json"
            pool.write_text(
                "user@example.com----pass----client----refresh\n",
                encoding="utf-8",
            )

            outlook_mail.mark_result(
                state,
                "user+grok1@example.com",
                False,
                reason="token_refresh_failed_http_400: AADSTS70000",
                login_email="user@example.com",
            )

            stats = outlook_mail.pool_stats(
                pool,
                state,
                {"outlook_alias_count": 2, "outlook_alias_include_original": True},
            )
            self.assertEqual(stats["available"], 0)
            self.assertEqual(stats["token_invalid"], 3)


if __name__ == "__main__":
    unittest.main()
