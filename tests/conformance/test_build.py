"""Build-ID discovery, schema extraction and redeploy recovery (hermetic)."""

from __future__ import annotations

import json

import pytest

from lose_it.core import _build, _decoder
from lose_it.core._http import LoseItError

SERVICE_URL = "https://www.loseit.com/web/service"
BASE = "https://d3hsih69yn4d89.cloudfront.net/web/"
NEW_STRONG = "11A17D1DFC9DAA0636F4DEA4FB06063B"
NEW_POLICY = "48B8773EE1A2315CAE592AD337015120"

NOCACHE = (
    "var Mb='default',fc='gecko1_8',_b='ie10',jc='en',"
    f"ic='{NEW_STRONG}',rc='216F8ADB12837AC74D823AFE03F69517';"
    "try{k([Mb,fc],ic);k([jc,fc],ic+kc);k([Mb,_b],rc)}catch(a){}"
)
BUNDLE_JS = (
    "function zud(){tud.call(this,Vh(),'remote_logging','9AB6A9ABEA1CB0CAC3CEB3A9F185574E',wud)}\n"
    f"function ier(){{nbr();tud.call(this,Vh(),null,'{NEW_POLICY}',mbr);jPv=a-1}}"
)
INCOMPATIBLE = (
    '//EX[2,1,["com.google.gwt.user.client.rpc.IncompatibleRemoteServiceException/3936916533",'
    '"This application is out of date, please click the refresh button on your browser."],0,7]'
)


@pytest.fixture(autouse=True)
def _fresh_build_state(monkeypatch, tmp_path):
    monkeypatch.setattr(_build, "_live", None)
    monkeypatch.setattr(_build, "_schemas_by_build", {})
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
    monkeypatch.delenv("LOSEIT_AUTO_REFRESH", raising=False)
    monkeypatch.setattr(_decoder, "_LIVE_LOOKUP_TRIED", set())


def _wrap_main(js: str) -> str:
    return f"web.onScriptDownloaded({json.dumps([js])});"


def test_parse_strong_name_resolves_hoisted_permutation():
    assert _build.parse_strong_name(NOCACHE) == NEW_STRONG


def test_parse_policy_hash_ignores_remote_logging_proxy():
    assert _build.parse_policy_hash(BUNDLE_JS) == NEW_POLICY


def test_unwrap_bundle_handles_both_loader_styles():
    assert _build.unwrap_bundle(_wrap_main("a\nb")) == "a\nb"
    assert _build.unwrap_bundle("$wnd.web.runAsyncCallback2('x=\\'y\\';\\n')") == "x='y';\n"
    assert _build.unwrap_bundle('$wnd.web.runAsyncCallback3("q=\\"r\\"")') == 'q="r"'


def test_extract_schemas_reads_floats_longs_and_superclasses():
    js = (
        "function gP(a,b){POv(a==null||fP(a,b));return a}\n"
        "function LEd(a){var b;b=a.b[--a.a];if(b<0){return 1}return 2}\n"
        "function SEd(b,a){return a>0?b.d[a-1]:null}\n"
        "function UEd(a){var b;b=!!a.b[--a.a];return b}\n"
        "function VEd(a){var b;b=Number(a.b[--a.a]);return b}\n"
        "function WEd(a){var b;b=Number(a.b[--a.a]);return b}\n"
        "function IEd(a){if(a==36){return 62}return 63}\n"
        "function JEd(a){var c,d=0;while(d<a.length){c=IEd(a[d++])}return c}\n"
        "function XEd(b){var a=b.b[--b.a],c;c=JEd(a);return c}\n"
        "function sup(a,b){b.s=SEd(a,a.b[--a.a])}\n"
        "function de(a,b){var c;iPv[c=++jPv]=de;sup(a,b);b.a=gP(LEd(a),5);b.b=VEd(a);"
        "b.c=WEd(a);b.d=UEd(a);b.e=XEd(a);b.f=a.b[--a.a];jPv=c-1}\n"
        "function inst(a){return new X}\n"
        "var t1='com.loseit.core.client.model.Thing/123';a[t1]=[inst,de];"
    )
    payload = _build.extract_schemas_from_js(js, NEW_STRONG)
    thing = payload["schemas"]["com.loseit.core.client.model.Thing/123"]
    assert thing["fields"] == ["STRING", "OBJECT", "DOUBLE", "DOUBLE", "BOOLEAN", "LONG", "RAW"]
    assert thing["is_enum"] is False
    assert payload["primitives"]["read_double"] == ["VEd", "WEd"]


def _mock_live_build(httpx_mock):
    httpx_mock.add_response(url=f"{BASE}web.nocache.js", text=NOCACHE)
    httpx_mock.add_response(url=f"{BASE}{NEW_STRONG}.cache.js", text=_wrap_main(BUNDLE_JS))


def test_post_rpc_adopts_live_build_and_resends(test_client, httpx_mock, monkeypatch):
    monkeypatch.setattr(_build, "_schemas_cached", lambda base, strong: {"schemas": {}})
    stale = test_client.config.policy_hash
    httpx_mock.add_response(url=SERVICE_URL, text=INCOMPATIBLE)
    _mock_live_build(httpx_mock)
    httpx_mock.add_response(url=SERVICE_URL, text="//OK[1,[],0,7]")

    out = test_client.http.post_rpc(f"7|0|3|{BASE}|{stale}|com.x|1|2|3|")

    assert out == "//OK[1,[],0,7]"
    retry = httpx_mock.get_requests(url=SERVICE_URL)[-1]
    assert f"|{NEW_POLICY}|" in retry.content.decode()
    assert stale not in retry.content.decode()
    assert retry.headers["x-gwt-permutation"] == NEW_STRONG
    assert test_client.config.policy_hash == NEW_POLICY


def test_post_rpc_raises_when_live_build_matches(test_client, httpx_mock, monkeypatch):
    monkeypatch.setattr(_build, "_schemas_cached", lambda base, strong: {"schemas": {}})
    monkeypatch.setattr(
        _build,
        "_live",
        _build.BuildIds(test_client.config.strong_name, test_client.config.policy_hash),
    )
    httpx_mock.add_response(url=SERVICE_URL, text=INCOMPATIBLE)
    httpx_mock.add_response(
        url=f"{BASE}web.nocache.js",
        text=NOCACHE.replace(NEW_STRONG, test_client.config.strong_name),
    )
    httpx_mock.add_response(
        url=f"{BASE}{test_client.config.strong_name}.cache.js",
        text=_wrap_main(BUNDLE_JS.replace(NEW_POLICY, test_client.config.policy_hash)),
    )
    with pytest.raises(LoseItError, match="IncompatibleRemoteServiceException"):
        test_client.http.post_rpc("7|0|1|x|")


def test_post_rpc_auto_refresh_can_be_disabled(test_client, httpx_mock, monkeypatch):
    monkeypatch.setenv("LOSEIT_AUTO_REFRESH", "0")
    httpx_mock.add_response(url=SERVICE_URL, text=INCOMPATIBLE)
    with pytest.raises(LoseItError, match="IncompatibleRemoteServiceException"):
        test_client.http.post_rpc("7|0|1|x|")
    assert len(httpx_mock.get_requests()) == 1


def test_post_rpc_rediscovers_when_memoized_build_is_also_stale(
    test_client, httpx_mock, monkeypatch
):
    """Process memo says build B; the server has moved on to C. Adopt C, not B."""
    monkeypatch.setattr(_build, "_schemas_cached", lambda base, strong: {"schemas": {}})
    monkeypatch.setattr(_build, "_live", _build.BuildIds("B" * 32, "C" * 32))
    httpx_mock.add_response(url=SERVICE_URL, text=INCOMPATIBLE)
    _mock_live_build(httpx_mock)
    httpx_mock.add_response(url=SERVICE_URL, text="//OK[1,[],0,7]")

    test_client.http.post_rpc(f"7|0|1|{test_client.config.policy_hash}|")

    assert test_client.config.policy_hash == NEW_POLICY
    assert test_client.config.strong_name == NEW_STRONG


def test_decoder_miss_stays_keyerror_when_discovery_fails(monkeypatch):
    monkeypatch.setattr(_decoder, "_CATALOG", None)

    def failing_refresh(base_url=_build.BASE_URL, *, force=False):
        raise _build.BuildDiscoveryError("offline")

    monkeypatch.setattr(_build, "refresh", failing_refresh)
    with pytest.raises(KeyError, match="No schema"):
        _decoder._schema_for("com.loseit.core.client.model.Nope/1")
    monkeypatch.setattr(_decoder, "_CATALOG", None)


def test_decoder_miss_loads_live_schemas(monkeypatch):
    fqcn = "com.loseit.core.client.model.Brand/1"
    monkeypatch.setattr(_decoder, "_CATALOG", None)

    def fake_refresh(base_url=_build.BASE_URL, *, force=False):
        _decoder.register_schemas({fqcn: {"fields": ["RAW"], "is_enum": False}})
        return _build.BuildIds(NEW_STRONG, NEW_POLICY)

    monkeypatch.setattr(_build, "refresh", fake_refresh)
    assert _decoder._schema_for(fqcn).fields == ["RAW"]
    monkeypatch.setattr(_decoder, "_CATALOG", None)
