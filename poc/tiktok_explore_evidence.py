"""Sanitize browser-observed TikTok Explore requests for safe review."""

import argparse
import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlsplit


SENSITIVE_FIELD_PARTS = ("cookie", "token", "authorization", "signature", "sign")
SENSITIVE_FIELD_NAMES = {"x-bogus", "x-dynosaur", "x-gnarly"}
SAFE_QUERY_VALUE_NAMES = {"categorytype", "pulltype"}
PAGINATION_FIELD_NAMES = {
    "cursor",
    "hasmore",
    "has_more",
    "offset",
    "next_cursor",
}
EXPLORE_ITEM_LIST_ENDPOINT = "https://www.tiktok.com/api/explore/item_list/"


def summarize_value(value: Any, sensitive: bool) -> dict[str, Any]:
    text = str(value)
    if sensitive:
        return {"redacted": True, "length": len(text)}
    return {"type": json_type(value), "length": len(text)}


def json_type(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, (int, float)):
        return "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, Mapping):
        return "object"
    if isinstance(value, list):
        return "array"
    return type(value).__name__


def is_sensitive_field(name: str) -> bool:
    normalized = name.lower()
    return normalized in SENSITIVE_FIELD_NAMES or any(
        part in normalized for part in SENSITIVE_FIELD_PARTS
    )


def summarize_headers(headers: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    return {
        name: summarize_value(value, is_sensitive_field(name))
        for name, value in sorted(headers.items(), key=lambda item: item[0].lower())
    }


def summarize_query(url: str) -> dict[str, dict[str, Any]]:
    query: dict[str, dict[str, Any]] = {}
    for name, value in parse_qsl(urlsplit(url).query, keep_blank_values=True):
        summary = summarize_value(value, is_sensitive_field(name))
        if name.lower() in SAFE_QUERY_VALUE_NAMES and value.isdecimal():
            summary["value"] = value
        query[name] = summary
    return dict(sorted(query.items()))


def endpoint_from_url(url: str) -> str:
    parts = urlsplit(url)
    return f"{parts.scheme}://{parts.netloc}{parts.path}"


def set_cookie_names(headers: Mapping[str, Any]) -> list[str]:
    names: list[str] = []
    for name, value in headers.items():
        if name.lower() != "set-cookie":
            continue
        for cookie in str(value).split(","):
            first_part = cookie.strip().split(";", 1)[0]
            cookie_name, separator, _ = first_part.partition("=")
            if separator and cookie_name:
                names.append(cookie_name)
    return sorted(set(names))


def summarize_response_body(body: Any) -> dict[str, Any]:
    if not isinstance(body, Mapping):
        return {"root_type": json_type(body), "length": len(str(body))}

    item_containers = {
        key: len(value) for key, value in body.items() if isinstance(value, list)
    }
    pagination: dict[str, dict[str, Any]] = {}
    for key, value in body.items():
        if key.lower() not in PAGINATION_FIELD_NAMES or not isinstance(
            value, (str, int, float, bool)
        ):
            continue
        if key.lower() in {"hasmore", "has_more"} and isinstance(value, bool):
            pagination[key] = {"type": "boolean", "value": value}
        else:
            pagination[key] = summarize_value(value, sensitive=True)
    return {
        "root_type": "object",
        "keys": sorted(body),
        "item_containers": item_containers,
        "pagination": pagination,
    }


def sanitize_request_evidence(
    *,
    action: str,
    category_name: str | None,
    request: Mapping[str, Any],
    response: Mapping[str, Any],
) -> dict[str, Any]:
    """Return a commit-safe structural record of one browser request/response."""
    url = str(request["url"])
    response_headers = response.get("headers", {})
    if not isinstance(response_headers, Mapping):
        raise TypeError("response headers must be a mapping")
    response_cookie_names = response.get("set_cookie_names")
    if not isinstance(response_cookie_names, list):
        response_cookie_names = set_cookie_names(response_headers)

    return {
        "action": action,
        "category_name": category_name,
        "request": {
            "endpoint": endpoint_from_url(url),
            "method": str(request["method"]).upper(),
            "query": summarize_query(url),
            "headers": summarize_headers(request.get("headers", {})),
        },
        "response": {
            "status": response["status"],
            "headers": summarize_headers(response_headers),
            "set_cookie_names": sorted(set(response_cookie_names)),
            "body": summarize_response_body(response.get("body")),
        },
    }


def headers_from_har(headers: Any) -> dict[str, str]:
    if not isinstance(headers, list):
        return {}
    return {
        str(header["name"]): str(header.get("value", ""))
        for header in headers
        if isinstance(header, Mapping) and isinstance(header.get("name"), str)
    }


def cookie_names_from_har(cookies: Any) -> list[str]:
    if not isinstance(cookies, list):
        return []
    return sorted(
        {
            cookie["name"]
            for cookie in cookies
            if isinstance(cookie, Mapping) and isinstance(cookie.get("name"), str)
        }
    )


def body_from_har(content: Any) -> Any:
    if not isinstance(content, Mapping) or content.get("encoding") == "base64":
        return None
    text = content.get("text")
    if not isinstance(text, str):
        return None
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return text


def sanitize_har_entries(
    har: Mapping[str, Any],
    *,
    action: str,
    category_name: str | None,
    response_sample: Any | None = None,
) -> list[dict[str, Any]]:
    """Extract commit-safe Explore request evidence from an in-memory HAR."""
    log = har.get("log")
    if not isinstance(log, Mapping) or not isinstance(log.get("entries"), list):
        raise ValueError("HAR must contain log.entries")

    evidence: list[dict[str, Any]] = []
    for entry in log["entries"]:
        if not isinstance(entry, Mapping):
            continue
        request = entry.get("request")
        response = entry.get("response")
        if not isinstance(request, Mapping) or not isinstance(response, Mapping):
            continue
        url = request.get("url")
        if (
            not isinstance(url, str)
            or endpoint_from_url(url) != EXPLORE_ITEM_LIST_ENDPOINT
        ):
            continue
        body = body_from_har(response.get("content"))
        body_source = "har" if body is not None else "not_captured"
        if body is None and response_sample is not None:
            body = response_sample
            body_source = "response_sample"
        record = sanitize_request_evidence(
            action=action,
            category_name=category_name,
            request={
                "url": url,
                "method": request.get("method", "GET"),
                "headers": headers_from_har(request.get("headers")),
            },
            response={
                "status": response.get("status"),
                "headers": headers_from_har(response.get("headers")),
                "set_cookie_names": cookie_names_from_har(response.get("cookies")),
                "body": body,
            },
        )
        record["response"]["body_source"] = body_source
        evidence.append(record)
    return evidence


def convert_har_file(
    input_path: Path,
    output_path: Path,
    *,
    action: str,
    category_name: str | None,
    response_sample_path: Path | None = None,
) -> int:
    """Read a private HAR once and write only its sanitized Explore evidence."""
    if input_path.resolve() == output_path.resolve():
        raise ValueError("input and output paths must differ")
    har = json.loads(input_path.read_text(encoding="utf-8"))
    if not isinstance(har, Mapping):
        raise ValueError("HAR root must be an object")
    response_sample = None
    if response_sample_path is not None:
        response_sample = json.loads(response_sample_path.read_text(encoding="utf-8"))
    evidence = sanitize_har_entries(
        har,
        action=action,
        category_name=category_name,
        response_sample=response_sample,
    )
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        json.dumps(evidence, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return len(evidence)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Convert a local HAR into sanitized TikTok Explore evidence."
    )
    parser.add_argument("input", type=Path, help="Private HAR export; never commit it.")
    parser.add_argument(
        "output", type=Path, help="Destination for sanitized evidence JSON."
    )
    parser.add_argument(
        "--action", required=True, help="Browser action that produced the HAR."
    )
    parser.add_argument("--category-name", help="Visible category name, if applicable.")
    parser.add_argument(
        "--response-sample",
        type=Path,
        help="Private JSON response sample; never commit it.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        count = convert_har_file(
            args.input,
            args.output,
            action=args.action,
            category_name=args.category_name,
            response_sample_path=args.response_sample,
        )
    except (OSError, ValueError, json.JSONDecodeError):
        print("ERROR: HAR conversion failed without writing evidence.")
        return 2
    print(f"Wrote {count} sanitized Explore record(s) to {args.output}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
