# Runbook: Lose It! web redeploys (build IDs + parser schemas)

The SDK speaks Lose It!'s private GWT-RPC protocol. Every web redeploy can
change three things it depends on:

| What | Where it comes from | Symptom when stale |
|---|---|---|
| permutation strong name (`x-gwt-permutation`) | `web.nocache.js` | none observed (server accepts any permutation) |
| serialization policy hash (field 2 of every RPC body) | proxy constructor in `<strong_name>.cache.js` | `LoseItError("GWT error: …IncompatibleRemoteServiceException…")` |
| type signatures + field order (`_schemas.json`) | serializer table in the permutation bundle + `deferredjs/<strong_name>/N.cache.js` | decoder `KeyError: "No schema for 'com.loseit…/<crc>'"` |

## 1. Automatic recovery (default)

`lose_it.core._build` derives all three from the public CDN bundles:

- `HttpClient.post_rpc` catches `IncompatibleRemoteServiceException`,
  discovers the live strong name + policy hash, switches the client to them,
  and resends the envelope once.
- The decoder, on an unknown type signature, extracts the live build's
  schemas and overlays them on the bundled `_schemas.json`.

Discovery runs at most once per process (again only if the server rejects
the IDs it found), and extracted schemas are cached per build in
`$XDG_CACHE_HOME/lose_it/schemas-<strong_name>.json` (default
`~/.cache/lose_it/`). Pinned `LOSEIT_STRONG_NAME` / `LOSEIT_POLICY_HASH`
values are only a starting point; they no longer need hand-editing after a
redeploy. Set `LOSEIT_AUTO_REFRESH=0` to turn recovery off (hermetic tests,
or to surface redeploys as errors).

## 2. Refresh the bundled defaults

Auto-recovery costs one rejected RPC plus ~7 MB of bundle downloads the first
time a process meets a new build. To make the new build the default:

```bash
uv run loseit build --schemas-out src/lose_it/core/_schemas.json
#  strong_name    : 11A17D1DFC9DAA0636F4DEA4FB06063B
#  policy_hash    : 48B8773EE1A2315CAE592AD337015120
#  schemas        : 374
```

Copy `strong_name` and `policy_hash` into the `Settings` defaults in
`src/lose_it/core/_settings.py` (and `tests/conformance/test_settings.py`),
then:

```bash
uv run pytest
git checkout -b chore/refresh-gwt-build
git add src/lose_it/core/_schemas.json src/lose_it/core/_settings.py tests/conformance/test_settings.py
git commit -m "chore(parser): refresh defaults for Lose It! build <strong_name>"
```

`loseit build` without `--schemas-out` just prints the live IDs (no account
needed) — handy to check whether a deployment is behind.

## 3. How extraction works

- **Strong name:** `web.nocache.js` registers permutations as
  `k([<locale>,<user_agent>],<strong>)` with literals hoisted into variables;
  we resolve the `gecko1_8` user-agent permutation.
- **Policy hash:** the app's RPC proxy is constructed as
  `RemoteServiceProxy.call(this, moduleBase, null, '<policy>', serializer)`;
  GWT's `remote_logging` proxy has a non-null relative path and is ignored.
- **Schemas:** bundles are unwrapped from their loader calls; every fragment
  `deferredjs/<strong>/1..N.cache.js` is read until a 404. The minified
  stream-reader primitives are identified by their GWT template bodies (not
  their per-build names): object (`b=a.b[--a.a];if(b<0)`), string
  (`a>0?b.d[a-1]:null`), boolean (`!!a.b[--a.a]`), double *and* float
  (`Number(a.b[--a.a])` — there are two), and long (base64 decoder). Each
  `a[<type const>]=[instantiate, deserialize(, serialize)]` table entry is
  then read statement by statement, splicing superclass deserializers inline.

The previous extractor matched hard-coded minified names and recognized only
one of the two `Number(...)` readers, so types with `float` fields (for
example `DailyLogEntryWithMeals`) were missing fields in the bundled table.

## 4. Why a decoder at all

Before the schema-driven decoder, `loseit search "kodiak"` picked fields by
string-length heuristics (`brand = "Pancakes"`, `category = "Kodiak"`), which
could serialize the wrong `FoodIdentifier` back to the server. Reading fields
in declared order removes that class of bug.
