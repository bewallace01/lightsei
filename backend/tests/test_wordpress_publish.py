"""wordpress_publish: create a drafted SEO page on a WordPress site via REST.

Pure tests with a stubbed request seam (no network), mirroring
test_github_publish.py's approach for the git-deploy path.
"""
from __future__ import annotations

import pytest

import wordpress_publish as wp


# ---------- url normalization ---------- #


def test_normalize_base_url_adds_scheme_and_strips_slash():
    assert wp.normalize_base_url("example.com") == "https://example.com"
    assert wp.normalize_base_url("https://example.com/") == "https://example.com"
    assert wp.normalize_base_url("http://localhost:8080/blog/") == "http://localhost:8080/blog"


def test_normalize_base_url_strips_pasted_wp_json_root():
    assert wp.normalize_base_url("https://example.com/wp-json") == "https://example.com"
    assert wp.normalize_base_url("https://example.com/wp-json/wp/v2") == "https://example.com"


def test_normalize_base_url_rejects_garbage():
    assert wp.normalize_base_url("") is None
    assert wp.normalize_base_url("   ") is None


# ---------- payload ---------- #


def test_page_payload_includes_optional_fields_when_present():
    p = wp.page_payload(
        {"title": "T", "h1": "H", "slug": "my-slug", "meta_description": "m",
         "body_html": "<p>hi</p>"}, "draft")
    assert p == {"title": "T", "content": "<p>hi</p>", "status": "draft",
                 "slug": "my-slug", "excerpt": "m"}


def test_page_payload_omits_blank_optionals_and_falls_back_to_h1():
    p = wp.page_payload({"h1": "Headline", "body_html": "<p>x</p>"}, "publish")
    assert p["title"] == "Headline" and p["status"] == "publish"
    assert "slug" not in p and "excerpt" not in p


# ---------- publish ---------- #


class _FakeWP:
    def __init__(self, resp):
        self.resp = resp
        self.calls = []

    def __call__(self, *, method, url, username, app_password, json=None):
        self.calls.append({"method": method, "url": url, "username": username,
                           "app_password": app_password, "json": json})
        return self.resp


_PAGE = {"title": "Emergency Plumber Austin", "slug": "emergency-plumber-austin",
         "meta_description": "Fast help.", "h1": "Emergency Plumber",
         "body_html": "<p>We fix it.</p>"}


def test_publish_happy_path_returns_link_and_edit_url():
    fake = _FakeWP({"status": 201, "body": {
        "id": 42, "link": "https://example.com/emergency-plumber-austin/",
        "status": "draft"}})
    out = wp.publish_page_to_wordpress(
        request=fake, base_url="example.com", username="admin",
        app_password="abcd efgh", page=_PAGE)
    assert out["id"] == 42
    assert out["link"].endswith("/emergency-plumber-austin/")
    assert out["status"] == "draft"
    assert out["edit_url"] == "https://example.com/wp-admin/post.php?post=42&action=edit"
    # The call hit the pages REST endpoint with a draft payload + the creds.
    call = fake.calls[0]
    assert call["url"] == "https://example.com/wp-json/wp/v2/pages"
    assert call["json"]["status"] == "draft" and call["json"]["title"] == "Emergency Plumber Austin"
    assert call["username"] == "admin" and call["app_password"] == "abcd efgh"


def test_publish_defaults_to_draft_status():
    fake = _FakeWP({"status": 201, "body": {"id": 1, "link": "x", "status": "draft"}})
    wp.publish_page_to_wordpress(request=fake, base_url="example.com",
                                 username="u", app_password="p", page=_PAGE)
    assert fake.calls[0]["json"]["status"] == "draft"


def test_publish_rejects_bad_status():
    with pytest.raises(wp.WordPressPublishError):
        wp.publish_page_to_wordpress(request=_FakeWP({}), base_url="example.com",
                                     username="u", app_password="p", page=_PAGE,
                                     status="archived")


def test_publish_requires_credentials():
    with pytest.raises(wp.WordPressPublishError):
        wp.publish_page_to_wordpress(request=_FakeWP({}), base_url="example.com",
                                     username="", app_password="p", page=_PAGE)


def test_publish_requires_title_and_content():
    with pytest.raises(wp.WordPressPublishError):
        wp.publish_page_to_wordpress(request=_FakeWP({}), base_url="example.com",
                                     username="u", app_password="p",
                                     page={"body_html": "<p>x</p>"})  # no title
    with pytest.raises(wp.WordPressPublishError):
        wp.publish_page_to_wordpress(request=_FakeWP({}), base_url="example.com",
                                     username="u", app_password="p",
                                     page={"title": "T", "body_html": "  "})  # no content


def test_publish_maps_auth_failure_to_clear_message():
    fake = _FakeWP({"status": 401, "body": {"message": "nope"}})
    with pytest.raises(wp.WordPressPublishError) as e:
        wp.publish_page_to_wordpress(request=fake, base_url="example.com",
                                     username="u", app_password="bad", page=_PAGE)
    assert "credentials" in str(e.value).lower()


def test_publish_maps_404_to_rest_api_message():
    fake = _FakeWP({"status": 404, "body": {}})
    with pytest.raises(wp.WordPressPublishError) as e:
        wp.publish_page_to_wordpress(request=fake, base_url="example.com",
                                     username="u", app_password="p", page=_PAGE)
    assert "REST API" in str(e.value)


def test_publish_surfaces_other_errors_with_message():
    fake = _FakeWP({"status": 400, "body": {"message": "rest_invalid_param"}})
    with pytest.raises(wp.WordPressPublishError) as e:
        wp.publish_page_to_wordpress(request=fake, base_url="example.com",
                                     username="u", app_password="p", page=_PAGE)
    assert "rest_invalid_param" in str(e.value) and "400" in str(e.value)
