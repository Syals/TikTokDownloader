"""probe_category_sources 纯函数单测（不联网）。"""

from poc.explore.probe_category_sources import (
    analyze_category_payload,
    common_prefix_len,
    detect_challenge_signals,
    sort_ids,
)


def test_analyze_category_payload_groups_and_order():
    payload = {
        "v0": [
            {"text": "all", "name": "All", "type": "120"},
            {"text": "singing", "name": "Singing & Dancing", "type": "119"},
        ],
        "v1": [
            {"text": "comedy", "name": "Comedy(v1)", "type": "104"},
            {"text": "all-dup", "name": "All(v1)", "type": "120"},
        ],
    }
    result = analyze_category_payload(payload)
    assert result["group_count"] == 2
    assert result["groups"]["v0"]["types"] == ["120", "119"]
    assert result["groups"]["v1"]["types"] == ["104", "120"]
    # bar_order 取首组原序。
    assert result["bar_order"] == ["120", "119"]
    # 合并去重：同 ID 先到先得（v0 优先）。
    assert result["merged"] == {
        "120": "All",
        "119": "Singing & Dancing",
        "104": "Comedy(v1)",
    }
    assert result["merged_count"] == 3


def test_analyze_category_payload_skips_invalid_entries():
    payload = {
        "v0": [
            {"name": "NoType"},
            {"type": "104", "name": "  "},
            {"type": 112, "name": "Sports"},
            "garbage",
        ],
    }
    result = analyze_category_payload(payload)
    assert result["groups"]["v0"]["count"] == 4
    assert result["groups"]["v0"]["types"] == ["112"]
    assert result["merged"] == {"112": "Sports"}


def test_detect_challenge_signals():
    assert detect_challenge_signals(200, "https://www.tiktok.com/explore", "") == []
    signals = detect_challenge_signals(403, "https://www.tiktok.com/verify", "Captcha")
    assert "http_status_403" in signals
    assert "suspicious_url" in signals
    assert "captcha_marker" in signals
    # verify 出现在 URL 判可疑，但正文 verify 一词不误报。
    assert detect_challenge_signals(200, None, "verified badge") == []


def test_common_prefix_len():
    assert common_prefix_len(["120", "119", "104"], ["120", "119", "112"]) == 2
    assert common_prefix_len(["120"], ["120", "119"]) == 1
    assert common_prefix_len([], ["120"]) == 0


def test_sort_ids_numeric_order():
    assert sort_ids({"119", "100", "200", "104"}) == ["100", "104", "119", "200"]
