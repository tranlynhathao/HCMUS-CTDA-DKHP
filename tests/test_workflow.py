import json
import os
import tempfile
import unittest
from contextlib import ExitStack
from email.parser import BytesParser
from email.policy import default
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx

import register_courses
import fetch_courses


COURSES = {
    "Status": "OK",
    "Results": {
        "ListDaDangKy": [],
        "ListChuaDangKy": [{"MaMG": 123, "KyHieu": "CSC001", "TenMH": "Example course", "MaLopSH": "23CLC01"}],
    },
}
SETTINGS = {"PORTAL_USERNAME": "test_student", "PORTAL_PASSWORD": "test_password", "PORTAL_COURSES": "123"}


class ConfigTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(patch.dict(os.environ, SETTINGS, clear=True))
        self.stack.enter_context(patch("register_courses.load_dotenv"))

    def test_environment_override_and_deduplication(self):
        with patch.dict(os.environ, {"PORTAL_COURSES": "123, 456,123"}):
            self.assertEqual(register_courses.Config.from_env().courses, ["123", "456"])

    def test_fetch_does_not_require_course_selection(self):
        os.environ.pop("PORTAL_COURSES")
        self.assertEqual(fetch_courses.build_config().courses, [])
        with self.assertRaises(RuntimeError):
            register_courses.Config.from_env()

    def test_missing_credentials(self):
        os.environ["PORTAL_PASSWORD"] = " "
        with self.assertRaisesRegex(RuntimeError, "PORTAL_PASSWORD"):
            register_courses.Config.from_env()

    def test_invalid_ids_and_delays(self):
        for ids in ("CSC001", "0", "-1", "123;456"):
            with self.subTest(ids=ids), patch.dict(os.environ, {"PORTAL_COURSES": ids}):
                with self.assertRaises((RuntimeError, ValueError)):
                    register_courses.Config.from_env()
        for delay in ("0", "-1", "nan", "inf", "oops", "5"):
            with self.subTest(delay=delay), patch.dict(os.environ, {"PORTAL_DELAY_MIN": delay}):
                with self.assertRaises((RuntimeError, ValueError)):
                    register_courses.Config.from_env()


class FetchTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.directory = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        self.path = self.directory / "courses.json"
        self.path.write_text("previous good snapshot", encoding="utf-8")
        self.stack.enter_context(
            patch("fetch_courses.build_config", return_value=register_courses.Config("student", "secret", []))
        )
        self.stack.enter_context(patch("register_courses.TRUST_FILE", self.directory / ".2fa_device"))
        self.login = self.stack.enter_context(
            patch.object(register_courses.PortalClient, "ensure_logged_in", new_callable=AsyncMock, return_value=True)
        )
        self.actions = []
        self.reply = COURSES
        self.timing = {"Status": "OK"}
        real_client = httpx.AsyncClient

        def response(request):
            message = BytesParser(policy=default).parsebytes(
                b"Content-Type: " + request.headers["content-type"].encode() + b"\r\n\r\n" + request.content
            )
            fields = {
                part.get_param("name", header="content-disposition"): part.get_payload(decode=True).decode()
                for part in message.iter_parts()
            }
            self.actions.append(fields)
            action = fields["action"]
            if action == "checkThoiGianDangKy":
                return httpx.Response(200, json=self.timing)
            if action == "loadSinhVienInfo":
                return httpx.Response(200, json={"Status": "OK", "Results": {"MSSV": "student"}})
            self.assertEqual(action, "loadDangKyHocPhan")
            return httpx.Response(200, json=self.reply)

        self.stack.enter_context(
            patch(
                "register_courses.httpx.AsyncClient",
                side_effect=lambda **kwargs: real_client(transport=httpx.MockTransport(response), **kwargs),
            )
        )

    async def test_fetch_uses_only_read_actions_and_saves_response(self):
        result = await fetch_courses.fetch_monhoc(self.path)
        self.assertEqual(result, COURSES)
        self.assertEqual(json.loads(self.path.read_text()), COURSES)
        self.assertEqual(
            [row["action"] for row in self.actions], ["checkThoiGianDangKy", "loadSinhVienInfo", "loadDangKyHocPhan"]
        )
        self.assertEqual(self.actions[-1]["data"], "student")

    async def test_failed_response_preserves_previous_snapshot(self):
        for reply in (
            {"Status": "FAILED"},
            {"Status": "OK", "Results": {}},
            {"Status": "OK", "Results": {"ListDaDangKy": [], "ListChuaDangKy": [None]}},
        ):
            self.reply = reply
            with self.subTest(reply=reply), self.assertRaises(RuntimeError):
                await fetch_courses.fetch_monhoc(self.path)
            self.assertEqual(self.path.read_text(), "previous good snapshot")

    async def test_closed_registration_stops_before_loading_courses(self):
        self.timing = {"Status": "FAILED"}
        with self.assertRaises(RuntimeError):
            await fetch_courses.fetch_monhoc(self.path)
        self.assertEqual(len(self.actions), 1)
        self.assertEqual(self.path.read_text(), "previous good snapshot")

    async def test_login_failure_makes_no_data_requests(self):
        self.login.return_value = False
        with self.assertRaises(RuntimeError):
            await fetch_courses.fetch_monhoc(self.path)
        self.assertEqual(self.actions, [])

    async def test_failed_replace_preserves_snapshot_and_cleans_temp_file(self):
        with patch.object(Path, "replace", side_effect=OSError("write failed")):
            with self.assertRaises(OSError):
                fetch_courses.save_courses(self.path, COURSES)
        self.assertEqual(self.path.read_text(), "previous good snapshot")
        self.assertEqual(list(self.directory.iterdir()), [self.path])


if __name__ == "__main__":
    unittest.main()
