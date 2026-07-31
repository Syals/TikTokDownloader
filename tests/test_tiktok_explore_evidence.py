import json

from poc.explore.tiktok_explore_evidence import (
    sanitize_har_entries,
    sanitize_request_evidence,
)


def test_sanitize_request_evidence_redacts_sensitive_values() -> None:
    evidence = sanitize_request_evidence(
        action="select_category",
        category_name="Nature",
        request={
            "url": (
                "https://www.tiktok.com/api/explore/item_list/?cursor=20"
                "&msToken=secret-token&X-Bogus=signed-value"
                "&X-Dynosaur=dynamic-value"
            ),
            "method": "GET",
            "headers": {
                "Cookie": "sessionid=private; msToken=private-token",
                "Referer": "https://www.tiktok.com/explore/nature",
                "X-Gnarly": "another-signature",
            },
        },
        response={
            "status": 200,
            "headers": {
                "Content-Type": "application/json",
                "Set-Cookie": "msToken=rotated-token; Path=/; Secure",
            },
            "body": {
                "itemList": [
                    {"id": "123", "video": {"playAddr": "https://signed-url"}}
                ],
                "cursor": 40,
                "hasMore": True,
            },
        },
    )

    assert evidence["request"]["query"] == {
        "X-Bogus": {"redacted": True, "length": 12},
        "X-Dynosaur": {"redacted": True, "length": 13},
        "cursor": {"type": "string", "length": 2},
        "msToken": {"redacted": True, "length": 12},
    }
    assert evidence["request"]["headers"] == {
        "Cookie": {"redacted": True, "length": 40},
        "Referer": {"type": "string", "length": 37},
        "X-Gnarly": {"redacted": True, "length": 17},
    }
    assert evidence["response"] == {
        "status": 200,
        "headers": {
            "Content-Type": {"type": "string", "length": 16},
            "Set-Cookie": {"redacted": True, "length": 37},
        },
        "set_cookie_names": ["msToken"],
        "body": {
            "root_type": "object",
            "keys": ["cursor", "hasMore", "itemList"],
            "item_containers": {"itemList": 1},
            "pagination": {
                "cursor": {"redacted": True, "length": 2},
                "hasMore": {"type": "boolean", "value": True},
            },
        },
    }


def test_sanitize_request_evidence_omits_non_json_body_contents() -> None:
    evidence = sanitize_request_evidence(
        action="initial_load",
        category_name=None,
        request={
            "url": "https://www.tiktok.com/api/example/?aid=1988",
            "method": "GET",
            "headers": {},
        },
        response={
            "status": 200,
            "headers": {"Content-Type": "text/html"},
            "body": "<html>private response</html>",
        },
    )

    assert evidence["response"]["body"] == {
        "root_type": "string",
        "length": 29,
    }


def test_sanitize_har_entries_keeps_only_explore_evidence() -> None:
    response_sample = {
        "itemList": [{"id": "123", "video": {"playAddr": "https://signed-url"}}],
        "cursor": "8",
        "hasMore": True,
    }
    har = {
        "log": {
            "entries": [
                {
                    "request": {
                        "method": "GET",
                        "url": (
                            "https://www.tiktok.com/api/explore/item_list/"
                            "?categoryType=120&pullType=2&cursor=8"
                            "&msToken=private-token"
                        ),
                        "headers": [
                            {"name": "Cookie", "value": "sessionid=private"},
                            {
                                "name": "Referer",
                                "value": "https://www.tiktok.com/explore",
                            },
                        ],
                    },
                    "response": {
                        "status": 200,
                        "headers": [
                            {"name": "Content-Type", "value": "application/json"}
                        ],
                        "cookies": [{"name": "msToken", "value": "rotated-token"}],
                        "content": {},
                    },
                },
                {
                    "request": {
                        "method": "GET",
                        "url": "https://www.tiktok.com/api/recommend/item_list/",
                        "headers": [],
                    },
                    "response": {"status": 200, "headers": {}, "content": {}},
                },
            ]
        }
    }

    evidence = sanitize_har_entries(
        har,
        action="select_category",
        category_name="Comedy",
        response_sample=response_sample,
    )

    assert len(evidence) == 1
    assert evidence[0]["request"]["endpoint"] == (
        "https://www.tiktok.com/api/explore/item_list/"
    )
    assert evidence[0]["request"]["query"]["msToken"]["redacted"] is True
    assert evidence[0]["request"]["query"]["categoryType"]["value"] == "120"
    assert evidence[0]["request"]["query"]["pullType"]["value"] == "2"
    assert evidence[0]["response"]["set_cookie_names"] == ["msToken"]
    assert evidence[0]["response"]["body_source"] == "response_sample"
    assert evidence[0]["response"]["body"]["item_containers"] == {"itemList": 1}
    assert "private-token" not in json.dumps(evidence)
    assert "signed-url" not in json.dumps(evidence)
