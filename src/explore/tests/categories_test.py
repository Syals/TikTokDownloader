from pathlib import Path

from src.explore.categories import (
    build_category_slug_map,
    load_category_names,
    slugify_category_name,
)


def test_slugify_lowercases_and_folds_separators() -> None:
    assert slugify_category_name("Singing & Dancing") == "singing_dancing"
    assert slugify_category_name("Comedy") == "comedy"
    assert slugify_category_name("Anime and Cartoons") == "anime_and_cartoons"


def test_slugify_punctuation_only_is_empty() -> None:
    assert slugify_category_name("!!!") == ""


def test_load_category_names_filters_meta_and_invalid_keys(tmp_path: Path) -> None:
    target = tmp_path / "explore_categories.json"
    target.write_text(
        '{"_meta": {"source": "explore_ssr"}, "104": " Comedy ", '
        '"x": "bad", "": "empty"}',
        encoding="utf-8",
    )
    assert load_category_names(target) == {"104": "Comedy"}


def test_load_category_names_missing_file_returns_empty(tmp_path: Path) -> None:
    assert load_category_names(tmp_path / "absent.json") == {}


def test_load_category_names_rejects_non_dict_payload(tmp_path: Path) -> None:
    target = tmp_path / "explore_categories.json"
    target.write_text('["104"]', encoding="utf-8")
    assert load_category_names(target) == {}


def test_build_category_slug_map_uses_id_prefixed_slugs(tmp_path: Path) -> None:
    target = tmp_path / "explore_categories.json"
    target.write_text(
        '{"104": "Comedy", "119": "Singing & Dancing", "1": "!!!"}',
        encoding="utf-8",
    )
    assert build_category_slug_map(target) == {
        "104": "104-comedy",
        "119": "119-singing_dancing",
    }
