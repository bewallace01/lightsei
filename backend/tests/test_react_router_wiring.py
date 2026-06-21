"""Tests for react_router_wiring: turning a published page into a live route.

Pure string work, no DB or network.
"""
import react_router_wiring as rw


# A minimal App.tsx in the shape the helper targets: a run of lazy imports and
# a <Routes> block with a SiteLayout wrapper and a catch-all.
_APP = """import { lazy, Suspense } from 'react';
import { Routes, Route, Navigate } from 'react-router-dom';
import { SiteLayout } from './components/SiteLayout';
import { NotFoundPage } from './pages/NotFoundPage';

const HomePage = lazy(() =>
  import('./pages/HomePage').then((m) => ({ default: m.HomePage }))
);
const RestaurantWorkingCapitalPage = lazy(() =>
  import('./pages/RestaurantWorkingCapitalPage').then((m) => ({ default: m.RestaurantWorkingCapitalPage }))
);

export default function App() {
  return (
    <Routes>
      <Route element={<SiteLayout />}>
        <Route path="/" element={<HomePage />} />
        <Route path="/restaurant-working-capital" element={<RestaurantWorkingCapitalPage />} />
        <Route path="/blog/:slug" element={<BlogPostPage />} />
        <Route path="*" element={<NotFoundPage />} />
      </Route>
    </Routes>
  );
}
"""

_PAGE = "export function RestaurantInventoryTipsPage() {\n  return <div>hi</div>;\n}\n"


def test_parse_component_name_variants():
    assert rw.parse_component_name("export function FooPage(){}") == "FooPage"
    assert rw.parse_component_name("export const BarPage = () => {}") == "BarPage"
    assert rw.parse_component_name("export default function BazPage() {}") == "BazPage"
    assert rw.parse_component_name("const Nope = 1") is None


def test_import_specifier_for():
    assert rw.import_specifier_for("src/pages/FooPage.tsx") == "./pages/FooPage"
    assert rw.import_specifier_for("src/pages/Foo.jsx") == "./pages/Foo"
    assert rw.import_specifier_for("nope.txt") is None


def test_derive_route_path_matches_repo_convention():
    assert rw.derive_route_path("RestaurantWorkingCapitalPage") == "/restaurant-working-capital"
    assert rw.derive_route_path("RestaurantInventoryTipsPage") == "/restaurant-inventory-tips"


def test_plan_route_wiring_adds_import_and_route():
    plan = rw.plan_route_wiring(
        app_source=_APP, page_content=_PAGE,
        file_path="src/pages/RestaurantInventoryTipsPage.tsx")
    assert plan is not None
    assert plan["component"] == "RestaurantInventoryTipsPage"
    assert plan["route_path"] == "/restaurant-inventory-tips"
    out = plan["app_source"]
    # lazy import added, mirroring the existing style.
    assert "const RestaurantInventoryTipsPage = lazy(() =>" in out
    assert "import('./pages/RestaurantInventoryTipsPage').then((m) => ({ default: m.RestaurantInventoryTipsPage }))" in out
    # route added...
    assert '<Route path="/restaurant-inventory-tips" element={<RestaurantInventoryTipsPage />} />' in out
    # ...and it must come BEFORE the catch-all so it stays reachable.
    assert out.index('path="/restaurant-inventory-tips"') < out.index('path="*"')
    # exactly one new lazy const and one new route.
    assert out.count("= lazy(() =>") == _APP.count("= lazy(() =>") + 1
    assert out.count("<Route path=") == _APP.count("<Route path=") + 1


def test_plan_route_wiring_is_idempotent():
    plan = rw.plan_route_wiring(
        app_source=_APP, page_content=_PAGE,
        file_path="src/pages/RestaurantInventoryTipsPage.tsx")
    # Feeding the already-wired App back in yields no further change.
    assert rw.plan_route_wiring(
        app_source=plan["app_source"], page_content=_PAGE,
        file_path="src/pages/RestaurantInventoryTipsPage.tsx") is None


def test_plan_route_wiring_none_when_not_router_shape():
    assert rw.plan_route_wiring(
        app_source="<html><body>not an app</body></html>",
        page_content=_PAGE,
        file_path="src/pages/RestaurantInventoryTipsPage.tsx") is None


def test_plan_route_wiring_none_when_no_component_export():
    assert rw.plan_route_wiring(
        app_source=_APP, page_content="const x = 1;  // no export",
        file_path="src/pages/RestaurantInventoryTipsPage.tsx") is None
