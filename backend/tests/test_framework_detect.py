"""framework_detect: sniff a repo's framework to default the publish format."""
from __future__ import annotations

import json

import framework_detect as fd


def _pkg(deps=None, dev=None):
    return json.dumps({"dependencies": deps or {}, "devDependencies": dev or {}})


def test_next_app_router_by_dep_and_layout():
    paths = ["app/blog/page.tsx", "app/layout.tsx"]
    assert fd.detect_framework(paths, _pkg({"next": "14.0.0", "react": "18"})) == "next-app"


def test_next_app_router_detected_by_layout_even_without_package_json():
    # app/**/page.tsx is a Next App Router signature no other framework uses.
    assert fd.detect_framework(["app/about/page.tsx"], None) == "next-app"


def test_next_pages_router():
    paths = ["pages/index.tsx", "pages/about.tsx"]
    assert fd.detect_framework(paths, _pkg({"next": "13.4.0"})) == "next-pages"


def test_next_dep_but_no_pages_defaults_to_app():
    assert fd.detect_framework([], _pkg({"next": "14"})) == "next-app"


def test_vite_react_not_mistaken_for_next_pages():
    # Vite + React Router also keeps components under src/pages — must NOT be
    # called next-pages without the `next` dependency.
    paths = ["src/pages/HomePage.tsx", "src/pages/AboutPage.tsx"]
    fw = fd.detect_framework(paths, _pkg({"vite": "5", "react-router-dom": "6"}))
    assert fw == "vite-react"


def test_static_when_no_package_json():
    assert fd.detect_framework(["public/index.html", "about.html"], None) == "static"


def test_unknown_js_project():
    assert fd.detect_framework(["src/index.js"], _pkg({"lodash": "4"})) == "unknown"


def test_unparseable_package_json_falls_back_to_paths():
    # Garbage package.json -> deps empty -> path sniffing still finds App Router.
    assert fd.detect_framework(["app/x/page.jsx"], "{ not json") == "next-app"


def test_default_format_mapping():
    assert fd.default_format_for("next-app") == "next-app"
    assert fd.default_format_for("next-pages") == "next-pages"
    # Don't override for these: component mode / static keep the HTML default.
    assert fd.default_format_for("vite-react") is None
    assert fd.default_format_for("static") is None
    assert fd.default_format_for("unknown") is None
