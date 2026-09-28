from pathlib import Path

import pytest

from monitoring import (
    URLValidationError,
    assess_change,
    classify_business_changes,
    extract_visible_text,
    is_meaningful_change,
    normalize_text,
    text_change_score,
    validate_public_url,
)

FIXTURES = Path(__file__).parent / "fixtures"
PUBLIC_IP = "93.184.216.34"


def public_resolver(host: str, port: int) -> list[str]:
    assert host
    assert port > 0
    return [PUBLIC_IP]


def mixed_resolver(host: str, port: int) -> list[str]:
    return [*public_resolver(host, port), "10.0.0.5"]


@pytest.mark.parametrize(
    "url",
    [
        "ftp://example.com/file",
        "file:///etc/passwd",
        "javascript:alert(1)",
        "example.com",
        "https://user:password@example.com",
        "http://[::1",
        "http:\\\\127.0.0.1",
    ],
)
def test_url_validation_rejects_unsupported_urls(url: str) -> None:
    with pytest.raises(URLValidationError):
        validate_public_url(url, resolver=public_resolver)


@pytest.mark.parametrize(
    "url",
    [
        "http://localhost",
        "http://service",
        "http://printer.local",
        "https://api.internal",
        "http://127.0.0.1",
        "http://10.20.30.40",
        "http://172.16.0.1",
        "http://192.168.1.1",
        "http://169.254.169.254/latest/meta-data",
        "http://224.0.0.1",
        "http://[::1]",
        "http://[fe80::1]",
    ],
)
def test_url_validation_rejects_internal_destinations(url: str) -> None:
    with pytest.raises(URLValidationError):
        validate_public_url(url, resolver=public_resolver)


def test_url_validation_rejects_private_dns_results() -> None:
    with pytest.raises(URLValidationError):
        validate_public_url(
            "https://shop.example.com",
            resolver=mixed_resolver,
        )


def test_url_validation_accepts_public_dns_and_ipv6() -> None:
    assert (
        validate_public_url(
            "https://shop.example.com:8443/products",
            resolver=public_resolver,
        )
        == "https://shop.example.com:8443/products"
    )
    assert (
        validate_public_url("https://[2606:4700:4700::1111]/")
        == "https://[2606:4700:4700::1111]/"
    )


def test_redirect_destination_must_also_be_public() -> None:
    validate_public_url(
        "https://shop.example.com/redirect",
        resolver=public_resolver,
    )
    with pytest.raises(URLValidationError):
        validate_public_url("http://127.0.0.1/admin")


def test_visible_text_excludes_boilerplate_and_nonvisible_content() -> None:
    html = (FIXTURES / "store_before.html").read_text(encoding="utf-8")
    text = extract_visible_text(html)

    assert "Free shipping over" in text
    assert "$99" in text
    assert "Shop Now" in text
    assert "Catalog" not in text
    assert "Privacy" not in text
    assert "rotatingTrackingId" not in text
    assert "Internal experiment" not in text


def test_visible_text_excludes_nested_cookie_boilerplate() -> None:
    text = extract_visible_text(
        "<body><div class='cookie-banner'><span>Cookie settings</span></div>"
        "<main>Product details</main></body>"
    )
    assert text == "Product details"


def test_text_normalization_is_stable_and_deduplicates_lines() -> None:
    text = normalize_text(
        "  Shop   Now  \n"
        "Shop Now\n"
        "12 people are viewing this product\n"
        "© 2026 Northstar Supply\n"
    )
    assert text == "Shop Now"


def test_text_change_score_ignores_case_whitespace_and_punctuation() -> None:
    assert text_change_score("  SHOP   NOW! ", "shop now") == 0.0
    assert text_change_score("$99", "$79") > 0


def test_fixture_changes_are_meaningful_and_classified() -> None:
    old = extract_visible_text(
        (FIXTURES / "store_before.html").read_text(encoding="utf-8")
    )
    new = extract_visible_text(
        (FIXTURES / "store_after.html").read_text(encoding="utf-8")
    )

    assessment = assess_change(old, new)

    assert assessment.meaningful is True
    assert assessment.categories == ("PRICE", "SHIPPING", "CTA")
    assert assessment.text_score > 0


@pytest.mark.parametrize(
    ("old", "new", "category"),
    [
        ("$99", "$79", "PRICE"),
        ("Save 10 percent today", "Save 20 percent today", "PROMOTION"),
        ("Free shipping over $100", "Free shipping over $50", "SHIPPING"),
        ("Product is in stock", "Product is sold out", "PRODUCT"),
        ("Shop Now", "Start Shopping", "CTA"),
        ("Premium gear for teams", "Affordable gear for teams", "POSITIONING"),
    ],
)
def test_business_change_classification(
    old: str,
    new: str,
    category: str,
) -> None:
    assert category in classify_business_changes(old, new)


def test_trivial_changes_are_not_meaningful() -> None:
    assert not is_meaningful_change("Shop Now", "  SHOP NOW! ")
    assert not is_meaningful_change(
        "12 people are viewing this product",
        "15 people are viewing this product",
    )
    assert not is_meaningful_change("Welcome Alice", "Welcome Bob")


def test_large_visual_change_can_be_meaningful_without_text_change() -> None:
    assessment = assess_change(
        "Same page text",
        "Same page text",
        visual_score=8.0,
    )
    assert assessment.meaningful is True
    assert assessment.categories == ("OTHER",)
