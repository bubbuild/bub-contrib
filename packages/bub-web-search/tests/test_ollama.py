from bub_web_search import ollama


def test_format_search_results() -> None:
    result = ollama._format_search_results(
        [
            {
                "title": "Bub docs",
                "url": "https://example.com/docs",
                "content": "Official documentation.",
            },
            "invalid",
        ]
    )

    assert result == (
        "1. Bub docs\n   https://example.com/docs\n   Official documentation."
    )


def test_format_search_results_returns_none_without_valid_items() -> None:
    assert ollama._format_search_results(["invalid"]) == "none"
