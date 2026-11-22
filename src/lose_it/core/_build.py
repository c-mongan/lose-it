"""Discover the live Lose It! GWT web build and derive what the client needs.

Lose It!'s web app is a GWT 2.8 module. Every redeploy changes three things
this SDK depends on:

- the **permutation strong name** (``x-gwt-permutation`` header), published in
  ``web.nocache.js``;
- the **serialization policy hash** (field 2 of every RPC body), embedded in the
  permutation's ``<strong_name>.cache.js`` as an argument of the RPC proxy
  constructor;
- the **type signatures** (``fqcn/crc``) and field order of every serializable
  class, which :mod:`._decoder` reads from a schema table.

A stale policy hash makes the server answer ``IncompatibleRemoteServiceException``;
a changed class makes the decoder miss a schema. Both are recovered here from
the public, content-addressed bundles on Lose It!'s CDN, so the client can heal
itself at runtime instead of waiting for a release with new hard-coded IDs.

Discovery is read-only, unauthenticated, and cached: in memory per process and
on disk under ``$XDG_CACHE_HOME/lose_it`` (``~/.cache/lose_it``) keyed by strong
name, so a build's bundles are downloaded and parsed once.
"""

from __future__ import annotations

import json
import os
import re
import threading
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

from .._logging import logger
from ._settings import BASE_URL

_HEX32 = r"[0-9A-F]{32}"
_IDENT = r"[A-Za-z_$][\w$]*"
# The user-agent permutation whose bundle the SDK reads. Any permutation's
# strong name is accepted by the server (the policy, not the permutation, is
# what it validates); this one matches the historical default.
_USER_AGENT = "gecko1_8"
_MAX_FRAGMENTS = 64
_TIMEOUT = 30.0


class BuildDiscoveryError(RuntimeError):
    """The live build's identifiers or schemas could not be derived."""


@dataclass(frozen=True)
class BuildIds:
    strong_name: str
    policy_hash: str


# ── Fetching ─────────────────────────────────────────────────────────────────


def _base(base_url: str) -> str:
    return base_url if base_url.endswith("/") else base_url + "/"


def _fetch(url: str, *, allow_missing: bool = False) -> str | None:
    resp = httpx.get(url, timeout=_TIMEOUT, follow_redirects=True)
    if allow_missing and resp.status_code in (403, 404):
        return None
    if resp.status_code != 200:
        raise BuildDiscoveryError(f"GET {url} → HTTP {resp.status_code}")
    return resp.text


_JS_ESCAPES = {"n": "\n", "r": "\r", "t": "\t", "b": "\b", "f": "\f", "v": "\v", "0": "\0"}


def _js_string(inner: str) -> str:
    """Decode the body of a single-quoted JavaScript string literal."""

    def sub(m: re.Match[str]) -> str:
        esc = m.group(1)
        if esc[0] in "xu":
            return chr(int(esc[1:], 16))
        return _JS_ESCAPES.get(esc, esc)

    return re.sub(r"\\(x[0-9a-fA-F]{2}|u[0-9a-fA-F]{4}|.)", sub, inner, flags=re.DOTALL)


def unwrap_bundle(text: str) -> str:
    """Return the JavaScript inside a GWT bundle's loader call.

    Main bundles arrive as ``web.onScriptDownloaded(["…"])`` and deferred
    fragments as ``$wnd.web.runAsyncCallbackN("…")`` (or single-quoted).
    """
    m = re.match(r"^\s*[\w$.]+\((.*)\)\s*;?\s*$", text, re.DOTALL)
    if not m:
        raise BuildDiscoveryError("Unrecognized GWT bundle wrapper")
    arg = m.group(1).strip()
    if len(arg) >= 2 and arg[0] == arg[-1] == "'":
        return _js_string(arg[1:-1])
    try:
        value = json.loads(arg)
    except json.JSONDecodeError as exc:
        raise BuildDiscoveryError(f"Could not decode GWT bundle string: {exc}") from exc
    if isinstance(value, list):
        return "".join(value)
    if isinstance(value, str):
        return value
    raise BuildDiscoveryError("Unexpected GWT bundle payload type")


# ── Build identifiers ────────────────────────────────────────────────────────


def parse_strong_name(nocache_js: str, user_agent: str = _USER_AGENT) -> str:
    """Read the ``user_agent`` permutation's strong name from ``web.nocache.js``.

    The bootstrap registers permutations as ``k([<locale>,<ua>],<strong>)``
    with every literal hoisted into a short variable, so resolve the
    user-agent variable, then the first permutation registered for it.
    """
    ua = re.search(rf"(?<![\w$])({_IDENT})='{re.escape(user_agent)}'", nocache_js)
    if ua is None:
        raise BuildDiscoveryError(f"No {user_agent!r} permutation in web.nocache.js")
    reg = re.search(rf"{_IDENT}\(\[{_IDENT},{re.escape(ua.group(1))}\],({_IDENT})\)", nocache_js)
    if reg is None:
        raise BuildDiscoveryError("No permutation registration in web.nocache.js")
    val = re.search(rf"(?<![\w$]){re.escape(reg.group(1))}='({_HEX32})'", nocache_js)
    if val is None:
        raise BuildDiscoveryError("Permutation strong name literal not found")
    return val.group(1)


def parse_policy_hash(cache_js: str) -> str:
    """Read the serialization policy hash from a permutation bundle.

    GWT emits one proxy constructor per RPC service:
    ``RemoteServiceProxy.call(this, moduleBase, relativePath, '<policy>', serializer)``.
    The bundle also carries GWT's ``remote_logging`` service; the app's own
    service is the one whose entry point is set at runtime (``relativePath``
    is ``null``) and posts to ``/web/service``.
    """
    found = set(re.findall(rf"\.call\(this,[^,()]*\(\),null,'({_HEX32})',{_IDENT}\)", cache_js))
    if len(found) != 1:
        raise BuildDiscoveryError(f"Expected one RPC policy hash in the bundle, found {len(found)}")
    return found.pop()


def _nocache_url(base_url: str) -> str:
    return f"{_base(base_url)}web.nocache.js"


def _bundle_url(base_url: str, strong_name: str) -> str:
    return f"{_base(base_url)}{strong_name}.cache.js"


def _fragment_url(base_url: str, strong_name: str, n: int) -> str:
    return f"{_base(base_url)}deferredjs/{strong_name}/{n}.cache.js"


def discover_build_ids(base_url: str = BASE_URL) -> BuildIds:
    """Fetch the live strong name and policy hash (two unauthenticated GETs)."""
    nocache = _fetch(_nocache_url(base_url))
    assert nocache is not None
    strong_name = parse_strong_name(nocache)
    bundle = _fetch(_bundle_url(base_url, strong_name))
    assert bundle is not None
    return BuildIds(strong_name=strong_name, policy_hash=parse_policy_hash(unwrap_bundle(bundle)))


# ── Schema extraction ────────────────────────────────────────────────────────


def _iter_functions(js: str) -> Iterator[tuple[str, str]]:
    """Yield ``(name, source)`` for every ``function NAME(...) {...}`` in ``js``."""
    for header in re.finditer(rf"function ({_IDENT})\([^)]*\)\s*\{{", js):
        depth, i = 1, header.end()
        while i < len(js) and depth:
            c = js[i]
            if c == "{":
                depth += 1
            elif c == "}":
                depth -= 1
            i += 1
        yield header.group(1), js[header.start() : i]


def _body(source: str) -> str:
    return source[source.index("{") + 1 : -1]


def _find_primitives(functions: dict[str, str]) -> dict[str, list[str]]:
    """Name the build's GWT stream-reader primitives by their (stable) bodies.

    Function names are minified per build; what each primitive *does* is
    emitted from fixed GWT templates, so match on that.
    """
    shapes = {
        # value == null || canCast(value, typeId)
        "cast_assert": re.compile(rf"\(a==null\|\|{_IDENT}\(a,b\)\)"),
        # pop a token; negative = backref, else instantiate by type name
        "read_object": re.compile(r"b=a\.b\[--a\.a\];if\(b<0\)"),
        "read_string": re.compile(r"return a>0\?b\.d\[a-1\]:null"),
        "read_boolean": re.compile(r"b=!!a\.b\[--a\.a\]"),
        "read_double": re.compile(r"b=Number\(a\.b\[--a\.a\]\)"),
    }
    found: dict[str, list[str]] = {k: [] for k in (*shapes, "read_long")}
    for name, source in functions.items():
        body = _body(source)
        for kind, pat in shapes.items():
            if pat.search(body):
                found[kind].append(name)
    # Longs travel as base64 strings: a digit decoder (… '$'→62 …), a
    # string decoder looping over it, and the reader popping a token into it.
    digit = [n for n, s in functions.items() if "a==36" in s and "return 62" in s]
    decoders = [
        n for n, s in functions.items() for d in digit if f"{d}(" in _body(s) and "while" in s
    ]
    for name, source in functions.items():
        body = _body(source)
        if (
            any(re.search(rf"={re.escape(d)}\(a\)", body) for d in decoders)
            and "b.b[--b.a]" in body
        ):
            found["read_long"].append(name)
    missing = [k for k, v in found.items() if not v]
    if missing:
        raise BuildDiscoveryError(f"Could not locate GWT read primitives: {missing}")
    return found


def _field_kinds(
    source: str,
    prim: dict[str, list[str]],
    functions: dict[str, str],
    chain_cache: dict[str, list[str]],
) -> list[str]:
    """Return the field-read kinds of one deserializer, superclasses first-as-emitted."""

    def alt(kind: str) -> str:
        return "(?:" + "|".join(re.escape(n) for n in prim[kind]) + ")"

    obj = re.compile(rf"(?<![\w$]){alt('read_object')}\(a\)")
    string = re.compile(rf"(?<![\w$]){alt('read_string')}\(a,\s*a\.b\[--a\.a\]\)")
    boolean = re.compile(rf"(?<![\w$]){alt('read_boolean')}\(a\)")
    double = re.compile(rf"(?<![\w$]){alt('read_double')}\(a\)")
    long_ = re.compile(rf"(?<![\w$]){alt('read_long')}\(a\)")
    raw = re.compile(r"a\.b\[--a\.a\]")
    super_call = re.compile(rf"^({_IDENT})\(a\s*,\s*b\)$")

    fields: list[str] = []
    for stmt in (s.strip() for s in _body(source).split(";")):
        if not stmt or stmt.startswith("var "):
            continue
        sm = super_call.match(stmt)
        if sm:
            parent = sm.group(1)
            if parent not in chain_cache and parent in functions:
                chain_cache[parent] = _field_kinds(functions[parent], prim, functions, chain_cache)
            fields.extend(chain_cache.get(parent, []))
            continue
        if obj.search(stmt):
            fields.append("OBJECT")
        elif string.search(stmt):
            fields.append("STRING")
        elif boolean.search(stmt):
            fields.append("BOOLEAN")
        elif double.search(stmt):
            fields.append("DOUBLE")
        elif long_.search(stmt):
            fields.append("LONG")
        elif raw.search(stmt) and "=++" not in stmt:
            fields.append("RAW")
    return fields


def extract_schemas_from_js(all_js: str, strong_name: str) -> dict[str, Any]:
    """Build the ``_schemas.json`` payload from a build's unwrapped JavaScript."""
    functions: dict[str, str] = {}
    for name, source in _iter_functions(all_js):
        functions.setdefault(name, source)
    prim = _find_primitives(functions)

    constants = dict(
        re.findall(
            rf"(?<![\w$])({_IDENT})\s*=\s*"
            r"'((?:com\.loseit|com\.google|java\.|javax\.|\[(?:com\.loseit|com\.google|java\.|L))[^']+/\d+)'",
            all_js,
        )
    )
    table = {
        m.group(1): (m.group(2), m.group(3))
        for m in re.finditer(
            rf"(?<![\w$])a\[({_IDENT})\]\s*=\s*\[({_IDENT}),\s*({_IDENT})(?:\s*,\s*{_IDENT})?\]",
            all_js,
        )
    }
    enum_instantiate = re.compile(
        r"=\s*a\.b\[--a\.a\]\s*;[\s\S]*return\s+[A-Za-z_][\w$]*\s*\[\s*[a-z]\s*\]"
    )
    schemas: dict[str, dict[str, Any]] = {}
    chain_cache: dict[str, list[str]] = {}
    for var, fqcn in constants.items():
        entry = table.get(var)
        if entry is None or entry[1] not in functions:
            continue
        instantiate, deserialize = entry
        schemas[fqcn] = {
            "deserialize_fn": deserialize,
            "is_enum": bool(
                instantiate in functions and enum_instantiate.search(functions[instantiate])
            ),
            "fields": _field_kinds(functions[deserialize], prim, functions, chain_cache),
        }
    if not schemas:
        raise BuildDiscoveryError("No serializable types found in the GWT bundles")
    return {
        "permutation": strong_name,
        "primitives": {k: v[0] if len(v) == 1 else v for k, v in prim.items()},
        "schemas": schemas,
    }


def download_build_js(base_url: str, strong_name: str) -> str:
    """Download and unwrap the permutation bundle plus every deferred fragment."""
    main = _fetch(_bundle_url(base_url, strong_name))
    assert main is not None
    parts = [unwrap_bundle(main)]
    for n in range(1, _MAX_FRAGMENTS + 1):
        text = _fetch(_fragment_url(base_url, strong_name, n), allow_missing=True)
        if text is None:
            break
        parts.append(unwrap_bundle(text))
    return "\n".join(parts)


def extract_schemas(base_url: str, strong_name: str) -> dict[str, Any]:
    return extract_schemas_from_js(download_build_js(base_url, strong_name), strong_name)


# ── Cached runtime refresh ───────────────────────────────────────────────────

_lock = threading.Lock()
_live: BuildIds | None = None
_schemas_by_build: dict[str, dict[str, Any]] = {}


def cache_dir() -> Path:
    root = os.environ.get("XDG_CACHE_HOME") or str(Path("~/.cache").expanduser())
    return Path(root) / "lose_it"


def _schemas_cached(base_url: str, strong_name: str) -> dict[str, Any]:
    if strong_name in _schemas_by_build:
        return _schemas_by_build[strong_name]
    path = cache_dir() / f"schemas-{strong_name}.json"
    try:
        payload = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        logger.info("Extracting GWT schemas for Lose It! build {s}", s=strong_name)
        payload = extract_schemas(base_url, strong_name)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".tmp")
            tmp.write_text(json.dumps(payload, sort_keys=True))
            tmp.replace(path)
        except OSError as exc:
            logger.warning("Could not cache GWT schemas at {p}: {e}", p=path, e=exc)
    _schemas_by_build[strong_name] = payload
    return payload


def auto_refresh_enabled() -> bool:
    return os.environ.get("LOSEIT_AUTO_REFRESH", "1").strip().lower() not in ("0", "false", "no")


def refresh(base_url: str = BASE_URL, *, force: bool = False) -> BuildIds:
    """Discover the live build, load its schemas into the decoder, return its IDs.

    Memoized per process; ``force=True`` re-reads ``web.nocache.js`` (used when
    the server rejects the IDs a previous refresh produced, i.e. another deploy).
    """
    global _live
    from ._decoder import register_schemas  # decoder imports nothing from here

    with _lock:
        if _live is None or force:
            ids = discover_build_ids(base_url)
            register_schemas(_schemas_cached(base_url, ids.strong_name)["schemas"])
            if ids != _live:
                logger.warning(
                    "Lose It! web build is {s} (policy {p}); using it for RPCs",
                    s=ids.strong_name,
                    p=ids.policy_hash,
                )
            _live = ids
        return _live
