import json
from pathlib import Path

from poc.explore.tiktok_explore_replay import load_session


def test_load_session_accepts_logged_out_browser_session(project_tmp: Path) -> None:
    settings_path = project_tmp / "settings.json"
    settings_path.write_text(
        json.dumps(
            {
                "cookie_tiktok": {"msToken": "anonymous-token", "ttwid": "device"},
                "browser_info_tiktok": {"User-Agent": "Mozilla/5.0"},
            }
        ),
        encoding="utf-8",
    )

    cookie, user_agent = load_session(settings_path)

    assert cookie["msToken"] == "anonymous-token"
    assert user_agent == "Mozilla/5.0"
