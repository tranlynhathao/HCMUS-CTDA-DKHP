"""Fetch the current course-registration JSON from the CTDB HCMUS portal."""

from __future__ import annotations

import argparse
import asyncio
import json
import tempfile
from pathlib import Path

import httpx
from register_courses import PORTAL_HOST, PROJECT_DIR, REGISTER_URL, Config, PortalClient, setup_logging


DEFAULT_OUTPUT = PROJECT_DIR / "monhoc_current.json"


def build_config() -> Config:
    config = Config.from_env(require_courses=False)
    config.request_timeout = 15.0
    return config


def validate_courses(data: object) -> dict:
    if not isinstance(data, dict) or data.get("Status") != "OK":
        raise RuntimeError("Portal did not return a successful course list")
    results = data.get("Results")
    if not isinstance(results, dict):
        raise RuntimeError("Course response is missing Results")
    for key in ("ListDaDangKy", "ListChuaDangKy"):
        rows = results.get(key)
        if not isinstance(rows, list) or any(
            not isinstance(row, dict)
            or not str(row.get("MaMG", "")).isascii()
            or not str(row.get("MaMG", "")).isdigit()
            for row in rows
        ):
            raise RuntimeError(f"Course response contains invalid {key}")
    return data


def save_courses(output: Path, data: dict) -> None:
    validate_courses(data)
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        # Replace only after the entire validated response has been written.
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=output.parent, delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(json.dumps(data, ensure_ascii=False, indent=2) + "\n")
        temporary.replace(output)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


async def post_action(client: httpx.AsyncClient, action: str, data: str | None = None) -> dict:
    files = {"action": (None, action), "data": (None, "" if data is None else data)}
    headers = {
        "Accept": "*/*",
        "Referer": REGISTER_URL,
        "Origin": f"https://{PORTAL_HOST}",
        "X-OFFICIAL-REQUEST": "TRUE",
        "X-Requested-With": "XMLHttpRequest",
    }
    resp = await client.post(REGISTER_URL, files=files, headers=headers)
    resp.raise_for_status()
    try:
        payload = resp.json()
    except json.JSONDecodeError as e:
        raise RuntimeError(f"Portal returned non-JSON for {action}; the session may have expired") from e
    if not isinstance(payload, dict):
        raise RuntimeError(f"Portal returned an invalid response for {action}")
    return payload


async def fetch_monhoc(output: Path) -> dict:
    config = build_config()
    async with PortalClient(config) as portal:
        if not await portal.ensure_logged_in():
            raise RuntimeError("Login failed")

        timing = await post_action(portal.client, "checkThoiGianDangKy")
        if timing.get("Status") != "OK":
            raise RuntimeError("Registration is not available: checkThoiGianDangKy failed")

        student = await post_action(portal.client, "loadSinhVienInfo")
        if student.get("Status") != "OK":
            raise RuntimeError("loadSinhVienInfo failed")

        info = student.get("Results")
        if not isinstance(info, dict) or not info.get("MSSV"):
            raise RuntimeError("Student response is missing MSSV")
        mssv = str(info["MSSV"])
        courses = await post_action(portal.client, "loadDangKyHocPhan", mssv)
        save_courses(output, courses)
        return courses


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "-o", "--output", default=DEFAULT_OUTPUT, help="Output JSON path (default: repo/monhoc_current.json)"
    )
    return parser.parse_args()


async def main() -> None:
    setup_logging()
    args = parse_args()
    output = Path(args.output)
    data = await fetch_monhoc(output)
    results = data.get("Results") or {}
    da_dk = len(results.get("ListDaDangKy") or [])
    chua_dk = len(results.get("ListChuaDangKy") or [])
    print(f"Saved {output}")
    print(f"Status: {data.get('Status')}")
    print(f"ListDaDangKy: {da_dk}")
    print(f"ListChuaDangKy: {chua_dk}")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (RuntimeError, OSError, httpx.HTTPError) as error:
        raise SystemExit(str(error)) from None
    except KeyboardInterrupt:
        raise SystemExit(130) from None
