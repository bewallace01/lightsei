"""Wire a freshly published page into a React Router app's routing table.

When the SEO/design flow publishes a new page file into a repo whose pages are
registered in a central `src/App.tsx` (the common Vite + React Router shape:
a `const X = lazy(() => import('./pages/X')...)` block plus a
`<Route path="/x" element={<X />} />` entry), the page file alone is not
reachable. This module computes the two edits to App.tsx so the same publish PR
makes the page actually go live on merge.

Everything here is pure string work and best-effort: if the file doesn't match
the expected pattern, the functions return None and the caller publishes the
page file alone (the prior behavior), never breaking a publish.
"""
from __future__ import annotations

import re
from typing import Optional

# A page's exported component name. Matches the repo's `export function X()`,
# plus `export const X =` and `export default function X`.
_EXPORT_RES = (
    re.compile(r"export\s+function\s+([A-Z]\w+)\s*\("),
    re.compile(r"export\s+const\s+([A-Z]\w+)\s*[:=]"),
    re.compile(r"export\s+default\s+function\s+([A-Z]\w+)\s*\("),
)

# A lazy-import block:
#   const Foo = lazy(() =>
#     import('./pages/Foo').then((m) => ({ default: m.Foo }))
#   );
_LAZY_BLOCK_RE = re.compile(
    r"(?m)^[ \t]*const \w+ = lazy\(\(\) =>\s*?\n"
    r"[ \t]*import\([^\n]*\n"
    r"[ \t]*\);"
)

# A page route line:  <Route path="/foo" element={<Foo />} />
_ROUTE_LINE_RE = re.compile(
    r'(?m)^[ \t]*<Route\s+path="[^"]*"\s+element=\{<\w+\s*/>\}\s*/>\s*$'
)


def parse_component_name(content: str) -> Optional[str]:
    """The exported component name of a generated page file, or None."""
    for rx in _EXPORT_RES:
        m = rx.search(content or "")
        if m:
            return m.group(1)
    return None


def import_specifier_for(file_path: str) -> Optional[str]:
    """Turn a published file path into the import specifier App.tsx would use.
    `src/pages/FooPage.tsx` -> `./pages/FooPage`. Best-effort; None if the path
    isn't under a recognizable source root."""
    p = (file_path or "").strip().lstrip("/")
    p = re.sub(r"\.(tsx|ts|jsx|js)$", "", p)
    if p.startswith("src/"):
        return "./" + p[len("src/"):]
    if p.startswith("pages/") or "/pages/" in p:
        i = p.find("pages/")
        return "./" + p[i:]
    return None


def _kebab(name: str) -> str:
    s = re.sub(r"Page$", "", name)              # RestaurantWorkingCapitalPage -> RestaurantWorkingCapital
    s = re.sub(r"(?<!^)(?=[A-Z])", "-", s)      # -> Restaurant-Working-Capital
    return s.lower()


def derive_route_path(component: str) -> str:
    """Mirror the repo convention: RestaurantWorkingCapitalPage ->
    /restaurant-working-capital."""
    return "/" + _kebab(component)


def _indent_of(line: str) -> str:
    return line[: len(line) - len(line.lstrip(" \t"))]


def wire_route_into_app(
    app_source: str,
    *,
    component: str,
    import_specifier: str,
    route_path: str,
) -> Optional[str]:
    """Return App.tsx with a lazy import + a <Route> added for `component`, or
    None if it can't be done safely (pattern not found, or already wired).

    Idempotent: if the component is already imported or the route path already
    exists, returns None (no change) so a re-publish doesn't duplicate lines."""
    src = app_source or ""
    if not component or not import_specifier or not route_path:
        return None
    # Already wired (or a conflicting route) -> leave it alone.
    if re.search(rf"\bconst {re.escape(component)} = lazy\(", src):
        return None
    if re.search(rf'path="{re.escape(route_path)}"', src):
        return None

    lazy_matches = list(_LAZY_BLOCK_RE.finditer(src))
    route_matches = list(_ROUTE_LINE_RE.finditer(src))
    if not lazy_matches or not route_matches:
        return None  # not the shape we know how to edit

    # Build the lazy block, mirroring the indentation of an existing one.
    sample = lazy_matches[-1].group(0)
    sample_lines = sample.splitlines()
    outer = _indent_of(sample_lines[0])
    inner = _indent_of(sample_lines[1]) if len(sample_lines) > 1 else outer + "  "
    lazy_block = (
        f"{outer}const {component} = lazy(() =>\n"
        f"{inner}import('{import_specifier}').then((m) => ({{ default: m.{component} }}))\n"
        f"{outer});"
    )
    insert_at = lazy_matches[-1].end()
    src = src[:insert_at] + "\n" + lazy_block + src[insert_at:]

    # Re-find routes (offsets shifted after the import insert) and add the route
    # line, mirroring the indentation of an existing route. Anchor on the last
    # NON-catch-all route so the new route lands among the content routes, not
    # after `<Route path="*">` (the NotFound fallback).
    route_matches = list(_ROUTE_LINE_RE.finditer(src))
    content_routes = [m for m in route_matches if 'path="*"' not in m.group(0)]
    last = (content_routes or route_matches)[-1]
    route_indent = _indent_of(last.group(0))
    route_line = (
        f"{route_indent}<Route path=\"{route_path}\" "
        f"element={{<{component} />}} />"
    )
    insert_at = last.end()
    src = src[:insert_at] + "\n" + route_line + src[insert_at:]
    return src


def plan_route_wiring(
    *, app_source: str, page_content: str, file_path: str,
) -> Optional[dict]:
    """High-level helper: given App.tsx, the new page's content, and where the
    page was published, return {component, route_path, app_source} for the
    rewritten App.tsx, or None if wiring isn't applicable/safe."""
    component = parse_component_name(page_content)
    import_specifier = import_specifier_for(file_path)
    if not component or not import_specifier:
        return None
    route_path = derive_route_path(component)
    new_app = wire_route_into_app(
        app_source, component=component,
        import_specifier=import_specifier, route_path=route_path)
    if new_app is None:
        return None
    return {"component": component, "route_path": route_path, "app_source": new_app}
