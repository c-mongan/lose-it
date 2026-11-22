"""Thin httpx wrapper that posts GWT-RPC envelopes to ``/web/service``.

Provides one method, :meth:`HttpClient.post_rpc`, that handles:

- the constant GWT headers (``content-type``, ``x-gwt-permutation``, etc.),
- attaching the ``liauth`` cookie to every request,
- recognizing GWT-level ``//EX`` error responses and surfacing them as
  exceptions instead of returning a body the caller has to re-check,
- recovering from a Lose It! web redeploy: an
  ``IncompatibleRemoteServiceException`` makes it adopt the live build's
  strong name + policy hash (see :mod:`._build`) and retry once.

Every call emits structured loguru events: TRACE dumps the full request
+ response (headers, cookies, body), DEBUG keeps a one-liner, INFO marks
the RPC + duration, ERROR captures non-OK responses with the body.
"""

from __future__ import annotations

import re
import threading
import time

import httpx

from .._logging import headers_enabled, logger
from . import _build
from ._config import Config

# A GWT-RPC envelope's first ``|``-delimited field is its method name as
# an integer pointing into the string table; extracting the method name
# for human-readable logs requires a tiny scan. The token list always
# contains the FQCN ("com.loseit...") followed by the method name (e.g.
# "searchFoods"), so we grab the next string after the service FQCN.
_GWT_METHOD_RE = re.compile(
    r"\|com\.loseit\.[^|]*\bLoseItRemoteService\b\|([A-Za-z][A-Za-z0-9_]*)\|"
)


def _extract_rpc_method(payload: str) -> str:
    """Best-effort extraction of the RPC method name from a GWT envelope."""
    match = _GWT_METHOD_RE.search(payload)
    return match.group(1) if match else "<unknown>"


def _format_headers(headers: object) -> str:
    """Render a header bag as ``key: value`` lines, one per line."""
    try:
        items = list(headers.items())  # type: ignore[attr-defined]
    except AttributeError:
        return repr(headers)
    return "\n".join(f"  {k}: {v}" for k, v in items)


def _format_cookies(cookies: object) -> str:
    """Render an httpx.Cookies bag for the TRACE log."""
    try:
        items = list(cookies.items())  # type: ignore[attr-defined]
    except AttributeError:
        return repr(cookies)
    return "\n".join(f"  {k}={v}" for k, v in items)


class LoseItError(RuntimeError):
    """GWT-level error (``//EX[…]``) or unexpected response shape."""


class LoseItAuthError(LoseItError):
    """HTTP 401/403 — token expired or invalid."""


class HttpClient:
    """Owns the httpx session, headers, and cookies for a Lose It! account."""

    def __init__(self, config: Config, token: str, *, transport: httpx.BaseTransport | None = None):
        self.config = config
        self._adopt_lock = threading.Lock()
        self.token = token
        headers = {
            "content-type": "text/x-gwt-rpc; charset=UTF-8",
            "x-gwt-module-base": config.base_url,
            "x-gwt-permutation": config.strong_name,
            "x-loseit-gwtversion": "devmode",
            "x-loseit-hoursfromgmt": str(config.hours_from_gmt),
            "origin": "https://www.loseit.com",
            "referer": "https://www.loseit.com/",
        }
        cookies = httpx.Cookies()
        cookies.set("liauth", token, domain="www.loseit.com", path="/")
        cookies.set("fn_auth", token, domain="www.loseit.com", path="/")
        self._client = httpx.Client(
            headers=headers,
            cookies=cookies,
            timeout=30.0,
            transport=transport,
        )
        logger.debug(
            "HttpClient ready: user={user!r} base={base} permutation={perm} timeout=30s",
            user=config.user_name,
            base=config.base_url,
            perm=config.strong_name,
        )

    def close(self) -> None:
        self._client.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    def post_rpc(self, payload: str) -> str:
        """POST a GWT-RPC envelope; return the ``//OK[…]`` response text.

        Raises :class:`LoseItAuthError` on 401/403, :class:`LoseItError` on
        any other non-OK response (including GWT-level ``//EX[…]`` errors).

        When the server rejects the build identifiers
        (``IncompatibleRemoteServiceException``), switch this client to the
        live build's identifiers and resend the envelope once with the new
        policy hash. Disable with ``LOSEIT_AUTO_REFRESH=0``.
        """
        # Policy hash is string-table entry 2 (``7|0|N|<base>|<policy>|…``).
        fields = payload.split("|", 5)
        sent_policy = self.config.policy_hash
        if len(fields) > 4 and re.fullmatch(r"[0-9A-F]{32}", fields[4]):
            sent_policy = fields[4]
        try:
            return self._post_once(payload)
        except LoseItError as exc:
            if "IncompatibleRemoteServiceException" not in str(exc):
                raise
            if not _build.auto_refresh_enabled():
                raise
            with self._adopt_lock:
                # A concurrent call may already have moved this client on.
                if self.config.policy_hash == sent_policy:
                    try:
                        adopted = self._adopt_live_build()
                    except (_build.BuildDiscoveryError, httpx.HTTPError) as discovery_exc:
                        logger.error("Lose It! build refresh failed: {e}", e=discovery_exc)
                        raise exc from discovery_exc
                    if not adopted:
                        raise
                current = self.config.policy_hash
            return self._post_once(payload.replace(f"|{sent_policy}|", f"|{current}|"))

    def _adopt_live_build(self) -> bool:
        """Point this client at the live build; False if it already was.

        Always re-reads ``web.nocache.js``: the server just rejected our IDs,
        so any memoized discovery may predate another redeploy.
        """
        current = (self.config.strong_name, self.config.policy_hash)
        ids = _build.refresh(self.config.base_url, force=True)
        if (ids.strong_name, ids.policy_hash) == current:
            return False
        self.config = self.config.model_copy(
            update={"strong_name": ids.strong_name, "policy_hash": ids.policy_hash}
        )
        self._client.headers["x-gwt-permutation"] = ids.strong_name
        return True

    def _post_once(self, payload: str) -> str:
        url = self.config.service_url
        method_name = _extract_rpc_method(payload)
        request_size = len(payload)

        if headers_enabled():
            logger.opt(lazy=True).trace(
                "HTTP REQUEST → {method_name}\n"
                "POST {url}\n"
                "── headers ──\n{headers}\n"
                "── cookies ──\n{cookies}\n"
                "── body ({size} bytes) ──\n{body}",
                method_name=lambda: method_name,
                url=lambda: url,
                headers=lambda: _format_headers(self._client.headers),
                cookies=lambda: _format_cookies(self._client.cookies),
                size=lambda: request_size,
                body=lambda: payload,
            )
        else:
            logger.opt(lazy=True).trace(
                "HTTP REQUEST → {method_name}\n"
                "POST {url}\n"
                "── body ({size} bytes) ──\n{body}\n"
                "(headers + cookies suppressed — re-run with --log-headers to include)",
                method_name=lambda: method_name,
                url=lambda: url,
                size=lambda: request_size,
                body=lambda: payload,
            )

        start = time.perf_counter()
        try:
            resp = self._client.post(url, content=payload)
        except httpx.HTTPError as exc:
            logger.error(
                "HTTP transport error on {method_name}: {err}",
                method_name=method_name,
                err=exc,
            )
            raise

        elapsed_ms = (time.perf_counter() - start) * 1000.0
        text = resp.text
        response_size = len(text)

        if headers_enabled():
            logger.opt(lazy=True).trace(
                "HTTP RESPONSE ← {method_name}\n"
                "{http_version} {status} ({elapsed:.1f} ms)\n"
                "── headers ──\n{headers}\n"
                "── body ({size} bytes) ──\n{body}",
                method_name=lambda: method_name,
                http_version=lambda: resp.http_version,
                status=lambda: resp.status_code,
                elapsed=lambda: elapsed_ms,
                headers=lambda: _format_headers(resp.headers),
                size=lambda: response_size,
                body=lambda: text,
            )
        else:
            logger.opt(lazy=True).trace(
                "HTTP RESPONSE ← {method_name}\n"
                "{http_version} {status} ({elapsed:.1f} ms)\n"
                "── body ({size} bytes) ──\n{body}",
                method_name=lambda: method_name,
                http_version=lambda: resp.http_version,
                status=lambda: resp.status_code,
                elapsed=lambda: elapsed_ms,
                size=lambda: response_size,
                body=lambda: text,
            )
        logger.debug(
            "rpc {method_name}: POST {url} → {status} in {elapsed:.1f} ms "
            "(req {req}B, resp {resp_size}B)",
            method_name=method_name,
            url=url,
            status=resp.status_code,
            elapsed=elapsed_ms,
            req=request_size,
            resp_size=response_size,
        )

        if resp.status_code in (401, 403):
            logger.error(
                "rpc {method_name}: auth failure HTTP {status} — token expired/invalid",
                method_name=method_name,
                status=resp.status_code,
            )
            raise LoseItAuthError(f"HTTP {resp.status_code}: token expired or invalid")
        if resp.status_code != 200:
            logger.error(
                "rpc {method_name}: HTTP {status} body={body!r}",
                method_name=method_name,
                status=resp.status_code,
                body=text[:200],
            )
            raise LoseItError(f"HTTP {resp.status_code}: {text[:200]}")
        if text.startswith("//EX"):
            match = re.search(r'"([^"]*)"', text)
            err_msg = match.group(1) if match else text[:200]
            logger.error(
                "rpc {method_name}: GWT //EX error: {err}",
                method_name=method_name,
                err=err_msg,
            )
            raise LoseItError(f"GWT error: {err_msg}")
        if not text.startswith("//OK"):
            logger.error(
                "rpc {method_name}: unexpected response shape: {head!r}",
                method_name=method_name,
                head=text[:200],
            )
            raise LoseItError(f"Unexpected response: {text[:200]}")
        logger.success(
            "rpc {method_name} OK in {elapsed:.1f} ms ({resp_size} bytes)",
            method_name=method_name,
            elapsed=elapsed_ms,
            resp_size=response_size,
        )
        return text
