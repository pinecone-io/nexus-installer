#!/usr/bin/env python3
"""Preflight validator — encode the consistency invariants as checks that FAIL before
install with a clear message, instead of at pod boot or first ingest.

STATIC checks (default, values-only, no cloud access):
  - dimension agreement: embedding.dimension == staticIndex.dimension ==
    indexMetadata.dimension == embeddingModel.dimension, and if the target dimension
    or index id differ from the OCI chart's baked values, FAIL for the OCI path with
    the explicit "OCI cannot override the data-plane dimension / index id" message.
  - container prefix: the seven containers derive from the stem.
  - self-hosted profile selected; every catalog api_key_ref has a providerKeys entry;
    all three chat tier slots (lite/standard/pro) + embedding + rerank resolve to a
    defined catalog entry.
  - catalog entry shape: every model entry satisfies the inference proxy's own schema
    (required fields present, ceilings positive, no forbidden field combination), so a
    hand-edited overlay fails here rather than crash-looping the proxy.
  - model ids vs litellm's registry, for api_style='litellm' entries ONLY (needs litellm
    installed; SKIPs with a re-run hint when it isn't): the surface's mode matches, a chat
    model carries the OpenAI params Nexus relies on, and its token budgets resolve — the
    questions the proxy itself puts to litellm at startup. An 'openai'-style entry is never
    looked up, because the proxy never looks it up either; its shape check above plus a
    live call are what cover it.
  - image registry override set; pull-secret server is a prefix of the registry base.
  - workload_identity: clientId set. shared_key: existingSecret set.
  - security: WARN when the NetworkPolicy enforcement check is turned off.
  - no leftover example/placeholder values (an `acme` token, an unfilled <...>, or a
    [YOURS] field still equal to customer.example.yaml).

LIVE gateway check (--live-gateway, opt-in, makes real HTTP calls):
  - mints an OAuth2 client_credentials token, then makes one 1-token chat completion
    and one tiny embedding call through the gateway — bodies shaped the way the
    inference proxy shapes them — so a wrong client secret / scope / gateway
    environment, a missing embeddings route, or a gateway that drops the
    `dimensions` request fails here in seconds instead of as 401s after the install.
    When inference.gateway.coversRerank is set, one rerank call rides the same token
    too, so the refreshing-token path is proven for rerank as well as chat + embed.

LIVE model check (--live-models, opt-in, makes real HTTP calls):
  - one real call for every model in the catalog, issued by the same client the proxy
    uses: litellm for an api_style='litellm' model (so litellm builds the route and picks
    the api_version, exactly as at runtime), and the gateway for the api_style='openai'
    entries a gateway fronts -- for those the call is the ONLY validation, so this runs
    the gateway probe itself rather than deferring to --live-gateway. The embedding leg
    MEASURES the returned vector width against embedding.dimension, which is the only way
    to be sure for a deployment name no lookup table knows. Needs litellm for the
    litellm-style legs; without it they SKIP.

LIVE checks (--live, opt-in, shells out to az/kubectl):
  - kube context reachable.
  - the seven blob containers exist.
  - every bundle image is present in the mirror at the expected tag.
  - the workload identity (resolved from its clientId) has a federated credential for
    each blob-accessing service account.

Exit 0 only if no check FAILs. WARN and SKIP never fail the run, but a SKIP means the
check did not run at all, so the summary reports those separately.

Usage: python3 preflight.py [-f customer.yaml] [--live]
"""
import argparse
import base64
import importlib.metadata
import json
import logging
import os
import re
import shlex
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request

try:
    import yaml
except ModuleNotFoundError:
    sys.exit(
        "PyYAML is required but not installed. From the install/ directory, run:\n"
        "  python3 -m venv .venv && source .venv/bin/activate && pip install -r requirements.txt"
    )

HERE = os.path.dirname(os.path.abspath(__file__))

CONTAINER_SUFFIXES = [
    "db",
    "nexus-source",
    "nexus-knowledge",
    "nexus-archive",
    "nexus-traces",
    "nexus-snapshots",
    "nexus-library",
]

# The six nexus stores derive as <bucketPrefix>-nexus-<store>; the DB shares <bucketPrefix>-db.
NEXUS_BUCKET_STORES = ["source", "knowledge", "archive", "traces", "snapshots", "library"]

# Nexus images the chart deploys + the DB set + FoundationDB. Used by the
# live image-presence check; image-manifest.sh derives the authoritative list from the render.
NEXUS_IMAGES = [
    "nexus_api", "nexus_orchestrator", "nexus_runtime", "nexus_gateway",
    "nexus_console", "nexus_mcp", "nexus_auth", "nexus_inference_proxy",
    "nexus_file_proxy",
]
DB_IMAGES = ["docs-api", "index-builder", "query-routers", "query-executors-slab", "request-log-writers"]

# The blob-accessing service accounts (namespace + release both "nexus"); each needs a
# federated credential or its pods 401. Mirrors terraform/aks-slim locals.tf.
BLOB_SERVICE_ACCOUNTS = [
    "nexus-api", "nexus-orchestrator",
    "docs-api-sa", "index-builders-slab-sa", "query-routers-sa",
    "query-executors-slab-sa", "request-log-writers-sa",
]

GREEN, RED, YELLOW, DIM, RESET = "\033[32m", "\033[31m", "\033[33m", "\033[2m", "\033[0m"
if not sys.stdout.isatty():
    GREEN = RED = YELLOW = DIM = RESET = ""

_fails = 0
_warns = 0
_skips = 0
_gateway_probed = False

_gen_dir = None


def load_gen(name):
    """Load a generated overlay (values.install.yaml etc.) if present, else None. Preflight
    validates the ACTUAL emitted artifacts so the checks catch generator/hand-edit drift,
    not just re-derived inputs."""
    if not _gen_dir:
        return None
    path = os.path.join(_gen_dir, name)
    if not os.path.exists(path):
        return None
    try:
        with open(path, encoding="utf-8") as f:
            return yaml.safe_load(f) or {}
    except (OSError, yaml.YAMLError):
        return None


def ok(msg):
    print(f"  {GREEN}PASS{RESET}  {msg}")


def fail(msg):
    global _fails
    _fails += 1
    print(f"  {RED}FAIL{RESET}  {msg}")


def warn(msg):
    global _warns
    _warns += 1
    print(f"  {YELLOW}WARN{RESET}  {msg}")


def skip(msg):
    """A check that could not run (an optional dependency is absent), as distinct from
    one that ran and passed. Counted separately so the summary can say so — a skipped
    check must never read as a clean pass."""
    global _skips
    _skips += 1
    print(f"  {DIM}SKIP{RESET}  {msg}")


def section(title):
    print(f"\n{title}")


def get(d, path, default=None):
    cur = d
    for part in path.split("."):
        if not isinstance(cur, dict) or part not in cur or cur[part] is None:
            return default
        cur = cur[part]
    return cur


def storage_provider(inp):
    return get(inp, "storage.provider", "abs")


def run(cmd):
    """Run a command, returning (rc, stdout). Never raises."""
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=120)
        return p.returncode, p.stdout.strip()
    except (subprocess.SubprocessError, FileNotFoundError, OSError) as e:
        return 1, str(e)


# ------------------------------------------------------------------ litellm (optional)
EXPECTED_LITELLM = "1.96.2"


def litellm_hint():
    """The command that re-runs THIS invocation with litellm present.

    Built from argv, not written out: a hardcoded `preflight.py` would send an operator
    who passed -f / --gen-dir to a different inputs file than the one they just
    validated. Only the litellm version is pinned -- it decides every registry answer,
    while the interpreter does not.
    """
    argv = [sys.argv[0] or "preflight.py"] + sys.argv[1:]
    rerun = " ".join(shlex.quote(a) for a in argv)
    return f"pip install 'litellm=={EXPECTED_LITELLM}' && python3 {rerun}"

_litellm = False  # False = not attempted yet; None = unavailable


def _litellm_version():
    """The installed litellm's version, or "" when it has no distribution metadata (a
    source checkout or a vendored copy) -- which is not a reason to stop checking."""
    try:
        return importlib.metadata.version("litellm")
    except importlib.metadata.PackageNotFoundError:
        return ""


def litellm_or_none():
    """The litellm module, or None when it isn't installed. Silences its console chatter
    (feedback banner, Info lines) so only preflight's own output shows, and warns once
    when the installed version isn't the one the proxy runs -- a different registry can
    answer differently, so a PASS against it is not proof the proxy will agree."""
    global _litellm
    if _litellm is not False:
        return _litellm
    # Left alone, litellm fetches the cost map from its GitHub main at import, so the
    # catalog would be checked against a moving registry nobody runs. The proxy reads the
    # map bundled in the wheel, and matching it is what makes pinning the version mean
    # anything.
    os.environ.setdefault("LITELLM_LOCAL_MODEL_COST_MAP", "true")
    try:
        import litellm
    except ImportError:
        _litellm = None
        return None
    litellm.suppress_debug_info = True
    for name in ("LiteLLM", "litellm"):
        logging.getLogger(name).setLevel(logging.CRITICAL)
    _litellm = litellm
    installed = _litellm_version()
    if installed and installed != EXPECTED_LITELLM:
        warn(
            f"litellm {installed} is installed but the inference proxy runs "
            f"{EXPECTED_LITELLM}; its model registry may differ, so a PASS below is not "
            f"proof the proxy agrees. Faithful check: {litellm_hint()}"
        )
    return _litellm


def litellm_model_info(model):
    """A model's entry in litellm's registry, or None when it has none -- the same call,
    and the same tolerance for a miss, that the inference proxy makes."""
    lite = litellm_or_none()
    if lite is None:
        return None
    try:
        info = lite.get_model_info(model=model)
    except Exception:
        return None
    return info if isinstance(info, dict) else None


# --------------------------------------------------------------------------- static
def _catalog_present(inp):
    """Whether there is an emitted catalog to judge, reporting the whole group once when
    there is not -- every member would otherwise repeat the same line."""
    if _catalog_targets(inp)[1] == "generated":
        return True
    section("Inference catalog (generated overlay)")
    skip("no generated/values.self-hosted.yaml — run gen-values.py first. The embedding "
         "width, catalog entry shape, model-id and --live-models checks all read it, so "
         "none of them ran; every other check is unaffected.")
    return False


def check_dimension(inp):
    section("Dimension agreement")
    dim = get(inp, "embedding.dimension")
    if dim is None:
        fail("embedding.dimension is not set")
        return
    baked_dim = get(inp, "bundle.bakedDimension")
    baked_id = get(inp, "bundle.bakedIndexId")
    idx_id = get(inp, "staticIndex.id")
    ok(f"embedding.dimension = {dim} (feeds staticIndex, indexMetadata, embeddingModel)")

    # The OCI path bakes the data-plane dimension + index id; it cannot override them.
    oci_dim_ok = int(dim) == int(baked_dim) if baked_dim is not None else False
    oci_id_ok = idx_id == baked_id
    if not oci_dim_ok:
        fail(
            f"target dimension {dim} != OCI chart baked dimension {baked_dim}. "
            "The OCI install path cannot override the data-plane dimension "
            "(PINECONE_HEADLESS__DIMENSION + the schema JSON are baked into the "
            "published db-slim values). Use the local-chart path (install.sh "
            "--path local --chart-path <chart checkout>, after "
            "remint-dbslim.sh sets the dimension), or a bundle chart built for this "
            "dimension. See README 'OCI vs local-chart path'."
        )
    else:
        ok(f"dimension {dim} == baked dimension {baked_dim} -> OCI path can carry it")

    # The catalog's dimension counts as a site: the proxy serves it from
    # GET /v1/models/embedding as the width to provision an index at, so one disagreeing
    # with the index the data plane builds puts vectors of one width into an index of
    # another.
    gi = load_gen("values.install.yaml")
    if gi:
        sites = {
            "staticIndex.dimension": get(gi, "staticIndex.dimension"),
            "nexus.config.indexMetadata.dimension": get(gi, "nexus.config.indexMetadata.dimension"),
            "nexus.config.embeddingModel.dimension": get(gi, "nexus.config.embeddingModel.dimension"),
        }
        for surface, cid, entry in _catalog_targets(inp)[0]:
            if surface == "embedding":
                sites[f"inference.embeddingModels.{cid}.dimension"] = entry.get("dimension")
        bad = {k: v for k, v in sites.items() if v is not None and int(v) != int(dim)}
        if bad:
            fail(f"dimension drift in the generated overlays vs embedding.dimension={dim}: {bad}")
        else:
            ok(f"generated overlays: all {len(sites)} dimension sites == {dim}")
        gid = get(gi, "staticIndex.id")
        gmid = get(gi, "nexus.config.indexMetadata.indexId")
        if gid != gmid:
            fail(f"index id drift in generated overlay: staticIndex.id={gid} != indexMetadata.indexId={gmid}")
    if not oci_id_ok:
        warn(
            f"staticIndex.id {idx_id} != baked index id {baked_id}. A fresh id also "
            "cannot be set over OCI (db-slim bakes PINECONE_HEADLESS__INDEX_ID) — "
            "install.sh will select the local-chart path automatically."
        )
    else:
        ok("staticIndex.id == baked index id -> OCI path can carry it")


def check_embedding_width(inp):
    """The declared width against whatever can establish it without a call.

    litellm's registry records an output_vector_size for some models and nothing for
    others (an `azure/<deployment>` entry has none), and the openai and pinecone styles
    are never looked up at all -- so for most catalogs the only thing that can confirm the
    width is --live-models, which measures the vector the model actually returns. This
    check states what it can and says so when it cannot, rather than guessing from the
    model's name.
    """
    section("Embedding model output width")
    targets, _source = _catalog_targets(inp)
    entries = [(cid, e) for surface, cid, e in targets if surface == "embedding"]
    if not entries:
        fail("the generated overlay declares no embedding model")
        return
    for cid, entry in entries:
        model = entry.get("model")
        declared = entry.get("dimension")
        asks_for_width = bool(entry.get("request_dimensions"))
        label = f"embedding '{cid}'"
        if not isinstance(declared, int) or declared <= 0:
            continue
        native = None
        if entry.get("api_style") == "litellm":
            native = (litellm_model_info(model) or {}).get("output_vector_size")
        if not isinstance(native, int) or native <= 0:
            detail = "asks the model to emit that width" if asks_for_width else "takes the model's native width"
            ok(f"{label}: declares {declared} and {detail}; only --live-models can confirm "
               "what the model returns")
            continue
        if native == declared:
            ok(f"{label}: {model!r} emits {native} natively == the declared {declared}")
        elif asks_for_width:
            ok(f"{label}: {model!r} emits {native} natively and request_dimensions asks for "
               f"{declared}; --live-models confirms the model honors it")
        else:
            fail(
                f"{label}: litellm records {model!r} as emitting {native}-wide vectors but "
                f"the entry declares {declared}, and request_dimensions is off, so nothing "
                f"reduces them. Set the dimension to {native}, or set request_dimensions "
                "if the model can emit a narrower width on request."
            )


def check_containers(inp):
    section("Container prefix")
    prefix = get(inp, "storage.containerPrefix")
    if not prefix:
        fail("storage.containerPrefix is empty (blob.abs.containerPrefix is required for abs)")
        return

    # Prefer the emitted overlay's containerPrefix so a generator/hand-edit drift is caught.
    ga = load_gen("values.abs.yaml")
    gen_prefix = get(ga, "blob.abs.containerPrefix") if ga else None
    if gen_prefix is not None and gen_prefix != prefix:
        fail(f"containerPrefix drift: input '{prefix}' != generated blob.abs.containerPrefix '{gen_prefix}'")
    eff = gen_prefix or prefix

    names = [f"{eff}-{s}" for s in CONTAINER_SUFFIXES]
    # Exactly the seven required containers, each a <stem>-<known-suffix>.
    if len(set(names)) != 7 or any(
        not n.startswith(f"{eff}-") or n[len(eff) + 1:] not in CONTAINER_SUFFIXES for n in names
    ):
        fail(f"container set is not the 7 required <stem>-{{{','.join(CONTAINER_SUFFIXES)}}}: {names}")
    else:
        ok(f"7 containers derive from stem '{eff}': {', '.join(names)}")
    if not get(inp, "storage.account"):
        fail("storage.account is required for Azure Blob (blob.abs.account)")


def check_inference(inp):
    section("Inference catalog / self-hosted profile")
    endpoint = get(inp, "inference.endpoint")
    chat = get(inp, "inference.chatDeployment")
    embed = get(inp, "inference.embeddingDeployment")
    rerank = get(inp, "inference.rerankDeployment")
    if not (endpoint and chat and embed and rerank):
        fail("inference.endpoint/chatDeployment/embeddingDeployment/rerankDeployment must all be set")
        return

    # Validate the ACTUAL emitted catalog when present; else reconstruct from inputs.
    gsh = load_gen("values.self-hosted.yaml")
    inf = get(gsh, "nexus.inference") if gsh else None
    if inf is None:
        inf = {
            "llmModels": {f"chat-{t}": {"api_key_ref": "llm-key"} for t in REQUIRED_LLM_TIERS},
            "embeddingModels": {embed: {"api_key_ref": "embedding-key"}},
            "rerankModels": {"rerank": {"api_key_ref": "rerank-key"}},
            "tiers": {**{t: f"chat-{t}" for t in REQUIRED_LLM_TIERS},
                      "embedding": embed, "rerank": "rerank"},
            "providerKeys": {"llm-key": "", "embedding-key": "", "rerank-key": ""},
        }

    profile = get(gsh, "nexus.configProfiles") if gsh else "self-hosted"
    if profile and "self-hosted" in str(profile):
        ok(f"self-hosted profile selected (configProfiles={profile})")
    else:
        fail(f"self-hosted profile NOT selected (configProfiles={profile!r}); the inference catalog is inert")

    models = {}
    for group in ("llmModels", "embeddingModels", "rerankModels"):
        models.update(inf.get(group, {}) or {})
    provider_keys = set((inf.get("providerKeys") or {}).keys())
    refs = {m.get("api_key_ref") for m in models.values() if isinstance(m, dict) and m.get("api_key_ref")}
    credentials = inf.get("credentials") or {}
    for cred in credentials.values():
        if isinstance(cred, dict):
            refs.update(
                cred[field]
                for field in ("client_id_ref", "client_secret_ref")
                if cred.get(field)
            )
    for m in models.values():
        if isinstance(m, dict):
            refs.update((m.get("extra_header_refs") or {}).values())
    missing = refs - provider_keys
    if missing:
        fail(f"credential ref(s) without a providerKeys entry: {sorted(missing)}")
    else:
        ok(f"every catalog credential ref has a providerKeys entry ({sorted(refs)})")

    dangling = {
        cid: m["credential_ref"]
        for cid, m in models.items()
        if isinstance(m, dict) and m.get("credential_ref")
        and m["credential_ref"] not in credentials
    }
    if dangling:
        fail(f"model(s) whose credential_ref names no credentials entry: {dangling}")
    elif credentials:
        ok(f"credential_ref(s) resolve to a defined credentials entry ({sorted(credentials)})")

    catalog_ids = set(models.keys())
    tiers = inf.get("tiers") or {}
    unresolved = {slot: cid for slot, cid in tiers.items() if cid not in catalog_ids}
    if unresolved:
        fail(f"tier slots referencing an undefined catalog entry: {unresolved}")
    else:
        ok("all tier slots resolve to a defined catalog entry")
    chat_tiers = [tiers.get(s) for s in REQUIRED_LLM_TIERS]
    if not all(chat_tiers):
        fail(f"chat tiers incomplete — lite/standard/pro must all be set, got {chat_tiers}")
    elif len(set(chat_tiers)) != len(chat_tiers):
        # Distinct because the picker's "lite == <id>" aliasing needs an injective mapping.
        collisions = {t: r for t, r in zip(REQUIRED_LLM_TIERS, chat_tiers)
                      if chat_tiers.count(r) > 1}
        fail(f"chat tiers must each point at a DISTINCT model, but {collisions} collide; "
             "the proxy raises on that at startup")
    else:
        ok("three chat tier slots configured (lite/standard/pro), each a distinct model")


# Mirrors the inference proxy's per-surface model schema.
REQUIRED_MODEL_FIELDS = {
    "chat": ("model", "api_style", "label", "provider"),
    "embedding": ("model", "api_style", "dimension", "max_input_chars", "max_batch_size"),
    "rerank": ("model", "api_style", "max_query_chars", "max_doc_chars", "max_docs_per_request"),
}
# Chat's tuple is empty because its budgets may still be default-filled from the registry.
POSITIVE_INT_FIELDS = {
    "chat": (),
    "embedding": ("dimension", "max_input_chars", "max_batch_size"),
    "rerank": ("max_query_chars", "max_doc_chars", "max_docs_per_request"),
}
PRICE_FIELDS = {
    "chat": ("input_price_per_mtok", "output_price_per_mtok",
             "cache_read_price_per_mtok", "cache_write_price_per_mtok"),
    "embedding": ("input_price_per_mtok",),
    "rerank": ("request_price_per_1k",),
}
# Every one of the proxy's config models is extra="forbid", so a field it does not declare
# is not ignored -- pydantic refuses to construct the model and the proxy never boots.
_SHARED_MODEL_FIELDS = frozenset({
    "model", "api_style", "api_key", "api_key_ref", "credential_ref", "base_url",
    "api_version", "extra_headers", "extra_header_refs", "max_retries", "available",
})
ALLOWED_MODEL_FIELDS = {
    "chat": _SHARED_MODEL_FIELDS | {
        "label", "provider", "vision", "model_family", "context_window",
        "max_output_tokens", "input_price_per_mtok", "output_price_per_mtok",
        "cache_read_price_per_mtok", "cache_write_price_per_mtok",
    },
    "embedding": _SHARED_MODEL_FIELDS | {
        "dimension", "max_input_chars", "max_batch_size", "request_dimensions",
        "input_price_per_mtok",
    },
    "rerank": _SHARED_MODEL_FIELDS | {
        "max_query_chars", "max_doc_chars", "max_docs_per_request", "request_price_per_1k",
    },
}
ALLOWED_CREDENTIAL_FIELDS = frozenset({
    "auth_style", "token_url", "client_id_ref", "client_secret_ref", "scope",
    "client_auth", "available",
})
NONEMPTY_WHEN_PRESENT = ("api_version", "credential_ref")
CREDENTIAL_AUTH_STYLES = ("oauth2_client_credentials",)
CREDENTIAL_CLIENT_AUTH = ("basic", "post")
CREDENTIAL_REQUIRED = ("auth_style", "token_url", "client_id_ref", "client_secret_ref", "scope")
REQUIRED_LLM_TIERS = ("lite", "standard", "pro")
# The proxy pins both when the entry names neither.
DEFAULT_PINECONE_BASE_URL = "https://api.pinecone.io"
DEFAULT_PINECONE_API_VERSION = "2025-10"
VALID_API_STYLES = {
    "chat": ("openai", "litellm"),
    "embedding": ("pinecone", "litellm", "openai"),
    "rerank": ("pinecone", "litellm"),
}
SURFACE_GROUPS = (("chat", "llmModels"), ("embedding", "embeddingModels"), ("rerank", "rerankModels"))


def _catalog_targets(inp):
    """([(surface, catalog_id, entry)], source) for every model in the emitted
    values.self-hosted.yaml -- the catalog that reaches the cluster, so a generator bug or
    a hand-edit is caught too. `source` is "generated", or "missing" when there is no
    overlay to read; every caller refuses to judge a catalog it had to invent. Surfaces are
    keyed by the proxy's own mode names, which is what the registry check compares against.
    """
    inf = get(load_gen("values.self-hosted.yaml"), "nexus.inference")
    if not inf:
        return [], "missing"
    targets = [
        (surface, cid, entry)
        for surface, group in SURFACE_GROUPS
        for cid, entry in (inf.get(group) or {}).items()
        if isinstance(entry, dict)
    ]
    return targets, "generated"

def check_catalog_structure(inp):
    """Config-shape invariants the proxy hard-fails on at startup that no live call can
    exercise: absent required fields, non-positive ceilings, and field combinations the
    schema forbids. gen-values.py gets these right, so this is a drift guard -- the
    local-chart path invites editing the emitted overlay by hand."""
    section("Catalog entry shape")
    targets, _source = _catalog_targets(inp)
    if not targets:
        fail("the generated overlay declares no inference models at all")
        return

    bad = 0
    for surface, cid, entry in targets:
        label = f"{surface} '{cid}'"
        api_style = entry.get("api_style")
        # Absent or blank, not merely falsy: a 0 is present, and the positive-int check
        # below is the one whose message fits it.
        missing = [
            f for f in REQUIRED_MODEL_FIELDS[surface]
            if entry.get(f) is None or (isinstance(entry.get(f), str) and not entry[f].strip())
        ]
        if missing:
            fail(f"{label}: required field(s) {missing} missing or empty — the proxy's "
                 "schema declares them with no default and won't construct the model")
            bad += 1
            continue
        unknown = sorted(set(entry) - ALLOWED_MODEL_FIELDS[surface])
        if unknown:
            fail(f"{label}: field(s) {unknown} are not in the proxy's schema for this "
                 "surface; it forbids extras, so a typo here fails startup rather than "
                 "being ignored")
            bad += 1
        for field in NONEMPTY_WHEN_PRESENT:
            if field in entry and not str(entry[field] or "").strip():
                fail(f"{label}: {field} is present but empty; the proxy accepts it absent "
                     "or non-empty, not blank")
                bad += 1
        if api_style not in VALID_API_STYLES[surface]:
            fail(f"{label}: api_style {api_style!r} is not valid for this surface "
                 f"(expected one of {list(VALID_API_STYLES[surface])})")
            bad += 1
            continue
        for field in POSITIVE_INT_FIELDS[surface]:
            value = entry.get(field)
            if not isinstance(value, int) or value <= 0:
                fail(f"{label}: {field} must be a positive integer, got {value!r}")
                bad += 1
        retries = entry.get("max_retries", 0)
        if not isinstance(retries, int) or retries < 0:
            fail(f"{label}: max_retries must be a non-negative integer, got {retries!r}")
            bad += 1
        if api_style == "openai" and not entry.get("base_url"):
            fail(f"{label}: api_style 'openai' requires base_url — the SDK appends the "
                 "route to it and derives no path of its own")
            bad += 1
        if surface == "chat":
            for field in ("context_window", "max_output_tokens"):
                value = entry.get(field)
                if field in entry and (not isinstance(value, int) or value <= 0):
                    fail(f"{label}: {field} is set to {value!r}; the proxy requires a "
                         "positive integer when the field is present, and rejects it "
                         "before any registry default-fill")
                    bad += 1
                elif field not in entry and api_style == "openai":
                    # No registry lookup happens for this style, so nothing default-fills
                    # the budgets and the proxy raises on whichever is unresolved.
                    fail(f"{label}: api_style 'openai' chat models must state {field} "
                         "(nothing default-fills it for this style)")
                    bad += 1
        if surface == "chat" and entry.get("model_family") not in (None, *MODEL_FAMILIES):
            fail(f"{label}: model_family {entry['model_family']!r} is not one of "
                 f"{sorted(MODEL_FAMILIES)}; the proxy's schema rejects it")
            bad += 1
        if surface == "rerank" and api_style == "litellm" and entry.get("api_version"):
            fail(f"{label}: api_version is valid only for api_style 'pinecone'; "
                 "litellm.arerank takes no such parameter and the proxy rejects it")
            bad += 1
        if entry.get("credential_ref") and (entry.get("api_key_ref") or entry.get("api_key")):
            fail(f"{label}: credential_ref and api_key_ref / api_key are mutually "
                 "exclusive — a model draws its bearer value from exactly one source")
            bad += 1
        if api_style == "pinecone":
            forbidden = [f for f in ("api_key_ref", "api_key", "credential_ref",
                                     "extra_headers", "extra_header_refs") if entry.get(f)]
            if forbidden:
                fail(f"{label}: {forbidden} are not valid for api_style 'pinecone' — "
                     "the caller supplies the key per request via the Api-Key header")
                bad += 1
        headers = entry.get("extra_headers") or {}
        header_refs = entry.get("extra_header_refs") or {}
        collision = sorted(set(headers) & set(header_refs))
        if collision:
            fail(f"{label}: header(s) {collision} set in both extra_headers and "
                 "extra_header_refs; declare each in one place only")
            bad += 1
        for field, mapping in (("extra_headers", headers), ("extra_header_refs", header_refs)):
            for name, value in mapping.items():
                if not name or not value:
                    fail(f"{label}: {field} entry {name!r} = {value!r} — header name and "
                         "value must both be non-empty")
                    bad += 1
        for field in PRICE_FIELDS[surface]:
            value = entry.get(field)
            if value is not None and (not isinstance(value, (int, float)) or value < 0):
                fail(f"{label}: {field} must be a number >= 0, got {value!r}")
                bad += 1

    bad += _check_credentials()
    if not bad:
        ok(f"all {len(targets)} catalog entries satisfy the proxy's schema")


def _check_credentials():
    """Every credentials entry against the proxy's schema for one. Returns the failure
    count. RFC 6749 §10.8 is why cleartext is refused: a token_url over http sends the
    client secret and the minted token in the clear, so the proxy admits it only on
    loopback."""
    credentials = get(load_gen("values.self-hosted.yaml"), "nexus.inference.credentials") or {}
    bad = 0
    for name, cred in credentials.items():
        label = f"credential '{name}'"
        if not isinstance(cred, dict):
            fail(f"{label}: not a mapping")
            bad += 1
            continue
        unknown = sorted(set(cred) - ALLOWED_CREDENTIAL_FIELDS)
        if unknown:
            fail(f"{label}: field(s) {unknown} are not in the proxy's credential schema; "
                 "it forbids extras, so a typo here fails startup")
            bad += 1
        missing = [f for f in CREDENTIAL_REQUIRED if not str(cred.get(f) or "").strip()]
        if missing:
            fail(f"{label}: required field(s) {missing} missing or empty")
            bad += 1
            continue
        if cred["auth_style"] not in CREDENTIAL_AUTH_STYLES:
            fail(f"{label}: auth_style {cred['auth_style']!r} is not one of "
                 f"{list(CREDENTIAL_AUTH_STYLES)}")
            bad += 1
        if cred.get("client_auth", "basic") not in CREDENTIAL_CLIENT_AUTH:
            fail(f"{label}: client_auth {cred['client_auth']!r} is not one of "
                 f"{list(CREDENTIAL_CLIENT_AUTH)}")
            bad += 1
        token_url = cred["token_url"]
        if not token_url.startswith(("http://", "https://")):
            fail(f"{label}: token_url must be an absolute http(s) URL, got {token_url!r}")
            bad += 1
        elif token_url.startswith("http://") and not _is_loopback(token_url):
            fail(f"{label}: token_url {token_url!r} is cleartext http to a non-loopback "
                 "host; the proxy refuses it because the client secret and the minted "
                 "token would cross the network in the clear")
            bad += 1
    return bad


def _is_loopback(url):
    host = urllib.parse.urlsplit(url).hostname or ""
    return host == "localhost" or host == "::1" or host.startswith("127.")


def _resolve_budget(configured, registry):
    """The budget the proxy ends up with: the registry
    value when nothing is configured, otherwise the configured one clamped down to the
    registry ceiling -- an operator can tighten a budget, not loosen it. Returns
    (value, clamped)."""
    ceiling = registry if isinstance(registry, int) and registry > 0 else None
    if not isinstance(configured, int) or configured <= 0:
        return ceiling, False
    if ceiling is not None and configured > ceiling:
        return ceiling, True
    return configured, False


def _check_registry_entry(surface, cid, entry):
    """One api_style='litellm' entry against litellm's registry, replicating what the
    proxy does with the answer at startup. Only that style reaches here: the proxy gates
    every lookup on it, so asking the registry about any other style would be
    inventing a verdict the proxy never forms — an `openai`-style `model` is the host's
    own deployment name, and a registry entry that happens to share it describes a
    different deployment."""
    label = f"{surface} '{cid}'"
    model = entry.get("model")
    info = litellm_model_info(model)

    if info is None:
        if surface == "chat" and not (entry.get("context_window") and entry.get("max_output_tokens")):
            fail(
                f"{label}: litellm's registry has no entry for {model!r}, so neither "
                "context_window nor max_output_tokens gets default-filled and the proxy "
                "raises 'no context_window resolved' at startup. Give the entry a model id "
                "litellm's registry carries, or state context_window and max_output_tokens "
                "on it."
            )
            return
        warn(
            f"{label}: litellm's registry has no entry for {model!r}. Current bundles "
            "accept that; an older one fails startup on it."
        )
        return

    mode = info.get("mode")
    if mode != surface:
        fail(f"{label}: litellm reports {model!r} has mode={mode!r}, but this surface "
             f"needs mode={surface!r}. The proxy raises on the mismatch at startup.")
        return

    if surface == "chat":
        # Computed by litellm at call time rather than stored in the registry JSON, so
        # this particular check exists only when the library is actually installed.
        supported = set(info.get("supported_openai_params") or [])
        absent = sorted({"tools", "response_format"} - supported)
        if absent:
            fail(f"{label}: {model!r} lacks OpenAI params Nexus relies on: {absent}. "
                 "The proxy rejects the model at startup.")
            return
        window, window_clamped = _resolve_budget(entry.get("context_window"), info.get("max_input_tokens"))
        output, output_clamped = _resolve_budget(entry.get("max_output_tokens"), info.get("max_output_tokens"))
        for field, value in (("context_window", window), ("max_output_tokens", output)):
            if not value:
                fail(f"{label}: {field} resolves to nothing — litellm knows {model!r} but "
                     f"records no {field}, so set it on the model entry.")
                return
        for field, clamped, ceiling in (
            ("context_window", window_clamped, window),
            ("max_output_tokens", output_clamped, output),
        ):
            if clamped:
                warn(f"{label}: configured {field} exceeds what litellm records for "
                     f"{model!r}; the proxy will clamp it to {ceiling}.")
        ok(f"{label}: {model!r} is mode=chat with tools + response_format "
           f"(context_window {window}, max_output_tokens {output})")
        return

    ok(f"{label}: {model!r} is mode={mode}")


def check_model_registry(inp):
    """Ask litellm what the inference proxy asks it at startup, so a bad model id fails
    here instead of crash-looping the proxy after a 25-minute install.

    api_style='litellm' entries only: that is the only style the proxy looks up, and
    asking the registry about any other would invent a verdict the proxy never forms.

    What the proxy does with the answer: a mode MISMATCH fails startup, as
    does a chat model whose supported_openai_params lack tools / response_format. A
    registry MISS is tolerated by current bundles -- except for a litellm-style chat
    model, where the miss also means no budget is default-filled and startup fails on
    the unresolved context_window.
    """
    section("Model ids vs litellm's registry")
    targets, _source = _catalog_targets(inp)

    # The three chat tiers name one deployment; look it up once.
    seen = set()
    routed = []
    for surface, cid, entry in targets:
        if entry.get("api_style") != "litellm":
            continue
        key = (surface, entry.get("model"))
        if key in seen:
            continue
        seen.add(key)
        routed.append((surface, cid, entry))

    if not routed:
        ok("no api_style='litellm' models in the catalog; the proxy looks nothing up")
        return
    if litellm_or_none() is None:
        skip(f"litellm is not installed, so {len(routed)} litellm-style model id(s) were "
             f"not checked against the registry the proxy consults. Re-run with: {litellm_hint()}")
        return
    print(f"  {DIM}(litellm {_litellm_version() or 'version unknown'}){RESET}")
    for surface, cid, entry in routed:
        _check_registry_entry(surface, cid, entry)


MODEL_FAMILIES = ("gpt5", "claude")


def infer_model_family(model):
    """The inference proxy's own rule, which is what
    decides the request quirks a chat call gets. `gpt5_series` counts: it is LiteLLM's own
    Azure routing spelling for the family. Deliberately excludes o-series and GPT-4o."""
    m = (model or "").lower()
    if m.startswith("gpt-5") or "/gpt-5" in m or "gpt-5." in m or "gpt5_series" in m:
        return "gpt5"
    if "claude" in m:
        return "claude"
    return None


def resolved_model_family(model, declared=None):
    """The family the proxy will act on: an operator-pinned `model_family` wins, since it
    exists precisely for a deployment name that hides the family (`chat-prod` fronting
    gpt-5), and inference from `model` fills in otherwise."""
    return declared or infer_model_family(model)


def _is_gpt5_family(model, declared=None):
    return resolved_model_family(model, declared) == "gpt5"


def _embedding_entry(inp):
    """The embedding entry the embedding tier resolves to, or {} when there is no
    overlay to read. Taking request_dimensions and dimension from here rather than from the
    inputs keeps the probe aligned with any catalog, however it was produced."""
    targets, source = _catalog_targets(inp)
    if source != "generated":
        return {}
    tiers = get(load_gen("values.self-hosted.yaml"), "nexus.inference.tiers") or {}
    wanted = tiers.get("embedding")
    entries = [(cid, e) for surface, cid, e in targets if surface == "embedding"]
    for cid, entry in entries:
        if cid == wanted:
            return entry
    return entries[0][1] if entries else {}


def _gateway_call_failed(url, status, body):
    """Report a non-200 gateway answer by failure class. True when the call failed."""
    if status == 200:
        return False
    if status == 0:
        fail(
            f"could not reach the gateway {url} at all: {body[:200]}. The token minted, "
            "so the credentials are fine — this host needs a firewall/DNS allowance to "
            "the gateway (a separate one from the authorization server), or run this "
            "check from a host that has it."
        )
    elif status == 401:
        fail(
            f"gateway returned 401 for {url}: {body[:200]}. The token minted, so this "
            "is the second credential (the subscription key) or an authorization "
            "scope that does not cover this product."
        )
    elif status == 404:
        fail(
            f"gateway returned 404 for {url}: {body[:200]}. inference.endpoint must be "
            "the gateway base up to but NOT including /deployments/, and the "
            "deployment name must match the gateway's own route."
        )
    else:
        fail(f"gateway returned HTTP {status} for {url}: {body[:200]}")
    return True


def check_live_gateway(inp):
    """Mint a token, then make one real chat and one real embedding call through the gateway.

    This is the cheap version of the failure it prevents: a wrong client secret,
    an unauthorized scope or the wrong gateway environment otherwise surfaces as
    401s from the proxy long after a 25-minute install has finished. Both bodies are
    shaped the way the inference proxy shapes them, so a green probe is evidence
    about the traffic the install will actually send.
    """
    global _gateway_probed
    _gateway_probed = True
    section("Gateway credentials (live)")
    gw = get(inp, "inference.gateway")
    if not gw:
        ok("no inference.gateway block; chat + embedding go straight to the provider")
        return

    scope = str(get(inp, "inference.gateway.scope") or "").strip()
    if not scope:
        fail(
            "inference.gateway.scope is not set. A client_credentials request that "
            "carries no scope is refused by the authorization server (Okta answers "
            "HTTP 400 invalid_scope), so there is nothing to probe with."
        )
        return
    api_version = str(get(inp, "inference.gateway.apiVersion") or "").strip()
    if not api_version:
        fail(
            "inference.gateway.apiVersion is not set. The gateway expects "
            "?api-version= on every call and rejects a request without it, so a probe "
            "without it would not resemble the install's traffic."
        )
        return

    token_url = get(inp, "inference.gateway.tokenUrl")
    client_id = os.environ.get(get(inp, "inference.gateway.clientIdEnv") or "", "")
    client_secret = os.environ.get(get(inp, "inference.gateway.clientSecretEnv") or "", "")
    if not (token_url and client_id and client_secret):
        fail(
            "inference.gateway needs tokenUrl plus the client id / secret present in "
            f"{get(inp, 'inference.gateway.clientIdEnv')!r} and "
            f"{get(inp, 'inference.gateway.clientSecretEnv')!r} in this shell"
        )
        return

    host = urllib.parse.urlsplit(token_url).hostname or ""
    if token_url.startswith("http://") and not (
        host in ("localhost", "::1") or host.startswith("127.")
    ):
        warn(
            f"inference.gateway.tokenUrl is http:// (host {host}), so the client secret "
            "crosses the network in cleartext on every token refresh. Use https:// unless "
            "this is a local stand-in gateway."
        )

    form = {"grant_type": "client_credentials", "scope": scope}
    headers = {"Content-Type": "application/x-www-form-urlencoded"}
    if (get(inp, "inference.gateway.clientAuth") or "basic") == "basic":
        basic = base64.b64encode(f"{client_id}:{client_secret}".encode()).decode()
        headers["Authorization"] = f"Basic {basic}"
    else:
        form["client_id"] = client_id
        form["client_secret"] = client_secret

    status, body = _http_post(token_url, urllib.parse.urlencode(form).encode(), headers)
    if status == 0:
        fail(
            f"could not reach the token endpoint {token_url} at all: {body[:200]}. This "
            "is a reachability problem, not a credential one — the credentials were "
            "never presented. This host needs a firewall/DNS allowance to the "
            "authorization server (a separate one from the gateway itself), or run "
            "this check from a host that has it."
        )
        return
    if status != 200:
        fail(
            f"token endpoint returned HTTP {status}: {body[:200]}. Check the client "
            "id / secret, the scope, and that the client is authorized for THIS "
            "gateway environment (a wrong environment answers like an unauthorized "
            "client)."
        )
        return
    try:
        payload = json.loads(body)
        token = payload["access_token"]
    except Exception:
        fail(f"token endpoint returned no access_token: {body[:200]}")
        return
    token_type = str(payload.get("token_type") or "").strip()
    if token_type and token_type.lower() != "bearer":
        fail(
            f"token endpoint returned token_type={token_type!r}. The proxy presents the "
            "token as `Authorization: Bearer`, so only a bearer token works on this path."
        )
        return
    ttl = payload.get("expires_in", "unset")
    ok(f"minted a token (expires_in={ttl}); the proxy refreshes it in-process")

    endpoint = (get(inp, "inference.endpoint") or "").rstrip("/")
    chat = get(inp, "inference.chatDeployment")
    embed = get(inp, "inference.embeddingDeployment")
    query = f"?api-version={urllib.parse.quote(api_version)}"
    call_headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {token}",
    }
    key_env = get(inp, "inference.gateway.subscriptionKeyEnv")
    if key_env:
        subscription_key = os.environ.get(key_env, "")
        if not subscription_key:
            fail(f"inference.gateway.subscriptionKeyEnv={key_env!r} is not set in this shell")
            return
        header = get(inp, "inference.gateway.subscriptionHeader") or "Ocp-Apim-Subscription-Key"
        call_headers[header] = subscription_key

    # The proxy sends the deployment as `model` and budgets with max_tokens, renaming it
    # only for a gpt-5-family model. A probe body that differs proves nothing about it.
    # The budget must cover a reasoning model's internal tokens, which are spent
    # before any visible output and count against it.
    budget = "max_completion_tokens" if _is_gpt5_family(chat) else "max_tokens"
    chat_url = f"{endpoint}/deployments/{chat}/chat/completions{query}"
    payload = json.dumps(
        {"model": chat, "messages": [{"role": "user", "content": "ping"}], budget: 512}
    ).encode()
    status, body = _http_post(chat_url, payload, call_headers)
    if status == 400 and ("max_tokens" in body or "output limit" in body):
        ok(f"chat probe hit the model's output limit ({chat_url}, {budget}) — auth and routing proven")
    elif not _gateway_call_failed(chat_url, status, body):
        ok(f"chat completion through the gateway succeeded ({chat_url}, {budget})")

    _check_gateway_embed(inp, endpoint, embed, query, call_headers)
    _check_gateway_rerank(inp, query, call_headers)


def _check_gateway_embed(inp, endpoint, embed, query, call_headers):
    """Its own function so an embed-specific early return (a dropped dimensions
    field) still leaves the rerank leg to run."""
    embed_url = f"{endpoint}/deployments/{embed}/embeddings{query}"
    embed_body = {"model": embed, "input": "ping"}
    catalog_embed = _embedding_entry(inp)
    want_dim = catalog_embed.get("dimension") if catalog_embed.get("request_dimensions") else None
    if want_dim is not None:
        embed_body["dimensions"] = want_dim
    status, body = _http_post(embed_url, json.dumps(embed_body).encode(), call_headers)
    if _gateway_call_failed(embed_url, status, body):
        return
    ok(f"embedding through the gateway succeeded ({embed_url})")
    if want_dim is None:
        return
    try:
        vector = json.loads(body)["data"][0]["embedding"]
    except (ValueError, TypeError, KeyError, IndexError):
        fail(f"embedding response from {embed_url} carries no data[0].embedding: {body[:200]}")
        return
    if len(vector) == int(want_dim):
        ok(f"gateway honored dimensions={want_dim} (returned a {len(vector)}-wide vector)")
    else:
        fail(
            f"asked the gateway for dimensions={want_dim} and got a {len(vector)}-wide "
            "vector: the request was dropped somewhere on the path, so every embedding "
            "would be the wrong width for the index. Either the gateway strips the field "
            "or the deployment cannot emit a narrower width on request — turn "
            f"request_dimensions off and set the dimension to {len(vector)}."
        )


def _check_gateway_rerank(inp, query, call_headers):
    """Only meaningful when coversRerank fronts rerank through the gateway; without
    it rerank uses a static key that never refreshes, the gap this proves gone. The
    model id travels in the body, so rerankEndpoint stops before /v2/rerank."""
    if not get(inp, "inference.gateway.coversRerank"):
        ok("inference.gateway.coversRerank is off; rerank does not ride the gateway, nothing to probe")
        return
    rerank_base = (get(inp, "inference.rerankEndpoint") or "").rstrip("/")
    rerank = get(inp, "inference.rerankDeployment")
    if not (rerank_base and rerank):
        fail(
            "inference.gateway.coversRerank is set but inference.rerankEndpoint / "
            "inference.rerankDeployment is missing, so rerank has no gateway route to probe"
        )
        return
    rerank_url = f"{rerank_base}/v2/rerank{query}"
    payload = json.dumps(
        {"model": rerank, "query": "ping", "documents": ["ping", "pong"], "top_n": 2}
    ).encode()
    status, body = _http_post(rerank_url, payload, call_headers)
    if _gateway_call_failed(rerank_url, status, body):
        return
    ok(f"rerank through the gateway succeeded ({rerank_url})")


def _http_post(url, data, headers, timeout=20):
    request = urllib.request.Request(url, data=data, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, response.read().decode(errors="replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode(errors="replace")
    except Exception as e:
        return 0, f"{type(e).__name__}: {e}"


def _classify_probe(status, detail):
    """Map a probe outcome to ("ok" | "warn" | "fail", reason). A 429 counts as proof:
    the provider had to authenticate the key before it could rate-limit it."""
    if status == 200:
        return "ok", "reachable, key accepted"
    if status in (401, 403):
        return "fail", f"auth rejected ({status}) -- bad key, or a key for another resource"
    if status in (400, 404, 422):
        return "fail", f"rejected ({status}) -- wrong model id, endpoint, or params: {detail}"
    if status == 429:
        return "ok", "rate-limited (429) -- the key is valid"
    if isinstance(status, int) and 500 <= status < 600:
        return "warn", f"provider error ({status}); could not verify: {detail}"
    return "warn", f"could not reach the provider: {detail}"


def _report_probe(label, status, detail, extra=""):
    """Print one probe verdict. True when the call proved the path works."""
    state, reason = _classify_probe(status, detail)
    if state == "fail":
        fail(f"{label}: {reason}")
        return False
    if state == "warn":
        warn(f"{label}: {reason}")
        return False
    ok(f"{label}: {reason}{extra}")
    return True


def _probe_key(inp, key_path, surface):
    """The provider key for one surface, or "" after reporting why it is missing."""
    key_env = get(inp, key_path)
    key = os.environ.get(key_env or "", "")
    if not key:
        fail(f"{key_path}={key_env!r} holds nothing in this shell, so {surface} cannot be "
             "probed. Export it and re-run.")
    return key


def _litellm_call(fn, **kwargs):
    """Run one litellm call the way the proxy's adapter does, reducing it to
    (status, detail). num_retries=0 keeps a dead endpoint from costing three timeouts;
    drop_params mirrors the adapter, which lets litellm discard params the route rejects."""
    try:
        return 200, "", fn(num_retries=0, drop_params=True, timeout=20, **kwargs)
    except Exception as e:
        detail = str(e)[:300]
        return _recover_status(getattr(e, "status_code", None), detail), detail, None


def _recover_status(status, detail):
    """litellm's rerank path reports every upstream failure as APIConnectionError with
    status_code 500, which would file a rejected key under "transient provider error" and
    let a broken credential pass as a warning. The upstream body survives in the message,
    so read the real status back out of it. Providers that answer in prose rather than a
    status (Voyage: `{"detail":"Provided API key is invalid."}`) are recognised by the
    wording -- a rejected key has to fail, not warn."""
    if status != 500:
        return status
    body = detail[detail.index("{"):] if "{" in detail else ""
    found = re.search(r"\b(4\d\d)\b", body)
    if found:
        return int(found.group(1))
    if re.search(r"api[ _-]?key|unauthori|authenticat|forbidden|invalid[ _-]token",
                 detail, re.IGNORECASE):
        return 401
    return status


def _probe_output_budget(model, configured=None):
    """The max_tokens the probe asks for: 512, which a reasoning model's internal tokens
    will not exhaust before any visible output, lowered to whatever the entry configures or
    the registry records -- the proxy clamps the same way, and an operator can tighten a
    budget, not loosen it."""
    info = litellm_model_info(model) or {}
    resolved, _ = _resolve_budget(configured, info.get("max_output_tokens"))
    if isinstance(resolved, int) and 0 < resolved < 512:
        return resolved
    return 512


# The env var each generated api_key_ref is fed from; only customer.yaml knows the name.
KEY_ENV_INPUTS = {
    "llm-key": "inference.llmKeyEnv",
    "embedding-key": "inference.embeddingKeyEnv",
    "rerank-key": "inference.rerankKeyEnv",
}
SURFACE_KEY_INPUTS = {
    "chat": "inference.llmKeyEnv",
    "embedding": "inference.embeddingKeyEnv",
    "rerank": "inference.rerankKeyEnv",
}


def _entry_key(inp, entry, surface):
    """The provider key for one catalog entry, or "" after reporting why it is missing."""
    path = KEY_ENV_INPUTS.get(entry.get("api_key_ref")) or SURFACE_KEY_INPUTS[surface]
    return _probe_key(inp, path, surface)


def _probe_chat_entry(inp, lite, entry):
    model = entry.get("model")
    key = _entry_key(inp, entry, "chat")
    if not key:
        return
    kwargs = {
        "model": model,
        "api_key": key,
        "messages": [{"role": "user", "content": "ping"}],
    }
    if entry.get("base_url"):
        kwargs["base_url"] = entry["base_url"]
    # The rename mirrors the proxy; the other spelling would probe a request shape the
    # install never sends.
    budget = "max_completion_tokens" if _is_gpt5_family(model, entry.get("model_family")) else "max_tokens"
    kwargs[budget] = _probe_output_budget(model, entry.get("max_output_tokens"))
    status, detail, _ = _litellm_call(lite.completion, **kwargs)
    _report_probe(f"chat {model!r}", status, detail, f" (via litellm, {budget})")


def _probe_embed_entry(inp, lite, entry):
    model = entry.get("model")
    key = _entry_key(inp, entry, "embedding")
    if not key:
        return
    declared = entry.get("dimension") if isinstance(entry.get("dimension"), int) else None
    asks_for_width = bool(entry.get("request_dimensions"))
    kwargs = {"model": model, "api_key": key, "input": ["ping"]}
    if entry.get("base_url"):
        # litellm's embedding entry point takes api_base, not base_url.
        kwargs["api_base"] = entry["base_url"]
    if asks_for_width and declared:
        kwargs["dimensions"] = declared
    status, detail, resp = _litellm_call(lite.embedding, **kwargs)
    if not _report_probe(f"embedding {model!r}", status, detail, " (via litellm)"):
        return
    # A 429 counts as a pass (the key was authenticated) but carries no vector to measure.
    if not declared or resp is None:
        return
    try:
        data = resp["data"] if isinstance(resp, dict) else resp.data
        width = len(data[0]["embedding"])
    except (TypeError, KeyError, IndexError, AttributeError):
        fail(f"embedding response for {model!r} carries no data[0].embedding")
        return
    _report_embed_width(model, declared, asks_for_width, width)


def _report_embed_width(model, declared, asks_for_width, width):
    # Compared whether or not a reduction was asked for: the invariant is the index width.
    if width == declared:
        how = f"honored dimensions={declared}" if asks_for_width else f"emits {width} natively"
        ok(f"{model!r} {how} -- matches the catalog's dimension")
    elif asks_for_width:
        fail(
            f"asked {model!r} for dimensions={declared} and got a {width}-wide vector, so "
            "every embedding would be the wrong width for the index. Either the deployment "
            "cannot emit a narrower width on request, or the field was dropped on the way "
            f"-- turn request_dimensions off and set the dimension to {width}."
        )
    else:
        fail(
            f"{model!r} returns {width}-wide vectors but the catalog declares {declared}, "
            "so ingest would write vectors the index cannot accept. Set "
            f"the dimension to {width}, or set request_dimensions if the model can emit a "
            "narrower width on request."
        )


def _openai_call(entry, key, route, body):
    """One request the way the proxy's openai adapter makes it: the SDK appends the route
    to base_url and derives no path of its own, api_version rides as the `api-version`
    query param, and extra_headers travel as default headers."""
    url = entry["base_url"].rstrip("/") + route
    if entry.get("api_version"):
        url += ("&" if "?" in url else "?") + "api-version=" + urllib.parse.quote(entry["api_version"])
    headers = {"Content-Type": "application/json", "Authorization": f"Bearer {key}"}
    headers.update(entry.get("extra_headers") or {})
    for header, ref in (entry.get("extra_header_refs") or {}).items():
        value = os.environ.get(ref, "")
        if not value:
            warn(f"extra_header_refs names env var {ref!r} for header {header!r}, which "
                 "holds nothing in this shell; the probe sends the call without it")
            continue
        headers[header] = value
    status, text = _http_post(url, json.dumps(body).encode(), headers)
    return url, status, text


def _probe_openai_chat_entry(inp, entry):
    model = entry.get("model")
    key = _entry_key(inp, entry, "chat")
    if not key:
        return
    budget = "max_completion_tokens" if _is_gpt5_family(model, entry.get("model_family")) else "max_tokens"
    configured = entry.get("max_output_tokens")
    body = {
        "model": model,
        "messages": [{"role": "user", "content": "ping"}],
        budget: min(configured, 512) if isinstance(configured, int) and configured > 0 else 512,
    }
    url, status, text = _openai_call(entry, key, "/chat/completions", body)
    _report_probe(f"chat {model!r}", status, text, f" ({url}, {budget})")


def _probe_openai_embed_entry(inp, entry):
    model = entry.get("model")
    key = _entry_key(inp, entry, "embedding")
    if not key:
        return
    declared = entry.get("dimension") if isinstance(entry.get("dimension"), int) else None
    asks_for_width = bool(entry.get("request_dimensions"))
    body = {"model": model, "input": "ping"}
    if asks_for_width and declared:
        body["dimensions"] = declared
    url, status, text = _openai_call(entry, key, "/embeddings", body)
    if not _report_probe(f"embedding {model!r}", status, text, f" ({url})"):
        return
    if not declared:
        return
    try:
        width = len(json.loads(text)["data"][0]["embedding"])
    except (ValueError, TypeError, KeyError, IndexError):
        fail(f"embedding response from {url} carries no data[0].embedding: {text[:200]}")
        return
    _report_embed_width(model, declared, asks_for_width, width)


def _pinecone_call(entry, key, route, body):
    """One request the way the proxy's pinecone adapter makes it: Api-Key plus a pinned
    X-Pinecone-API-Version, and base_url defaulted when the entry names none."""
    base = (entry.get("base_url") or DEFAULT_PINECONE_BASE_URL).rstrip("/")
    url = base + route
    headers = {
        "Content-Type": "application/json",
        "Api-Key": key,
        "X-Pinecone-API-Version": entry.get("api_version") or DEFAULT_PINECONE_API_VERSION,
    }
    status, text = _http_post(url, json.dumps(body).encode(), headers)
    return url, status, text


def _pinecone_key():
    """Pinecone-style models take the caller's key per request, so the catalog holds none.
    Probe with the deployment's own key from the environment."""
    key = os.environ.get("PINECONE_API_KEY", "")
    if not key:
        skip("PINECONE_API_KEY is not set in this shell, so the pinecone-style models were "
             "not called. They take the caller's key per request, so the catalog carries "
             "none to probe with.")
    return key


def _probe_pinecone_embed_entry(inp, entry, key):
    model = entry.get("model")
    declared = entry.get("dimension") if isinstance(entry.get("dimension"), int) else None
    url, status, text = _pinecone_call(entry, key, "/embed", {
        "model": model,
        "inputs": [{"text": "ping"}],
        "parameters": {"input_type": "passage", "truncate": "END"},
    })
    if not _report_probe(f"embedding {model!r}", status, text, f" ({url})"):
        return
    try:
        item = json.loads(text)["data"][0]
    except (ValueError, TypeError, KeyError, IndexError):
        fail(f"embedding response from {url} carries no data[0]: {text[:200]}")
        return
    if "sparse_values" in item or "sparse_indices" in item:
        fail(f"{model!r} returned a sparse embedding; the proxy rejects sparse models "
             "because Nexus has no slot for them — choose a dense Pinecone model")
        return
    if declared and "values" in item:
        _report_embed_width(model, declared, False, len(item["values"]))


def _probe_pinecone_rerank_entry(inp, entry, key):
    model = entry.get("model")
    url, status, text = _pinecone_call(entry, key, "/rerank", {
        "model": model,
        "query": "ping",
        "documents": [{"text": "ping"}, {"text": "pong"}],
        "return_documents": False,
        "parameters": {"truncate": "END"},
    })
    _report_probe(f"rerank {model!r}", status, text, f" ({url})")


def _probe_rerank_entry(inp, lite, entry):
    model = entry.get("model")
    key = _entry_key(inp, entry, "rerank")
    if not key:
        return
    kwargs = {
        "model": model,
        "api_key": key,
        "query": "ping",
        "documents": ["ping", "pong"],
        "return_documents": False,
    }
    if entry.get("base_url"):
        kwargs["api_base"] = entry["base_url"]
    status, detail, _ = _litellm_call(lite.rerank, **kwargs)
    where = entry.get("base_url") or "litellm's default endpoint for the provider"
    _report_probe(f"rerank {model!r}", status, detail, f" (via litellm, {where})")


PROBES = {"chat": _probe_chat_entry, "embedding": _probe_embed_entry, "rerank": _probe_rerank_entry}
OPENAI_PROBES = {"chat": _probe_openai_chat_entry, "embedding": _probe_openai_embed_entry}
PINECONE_PROBES = {"embedding": _probe_pinecone_embed_entry, "rerank": _probe_pinecone_rerank_entry}


def check_live_models(inp):
    """One tiny real call per model in the catalog -- so a bad or wrong-resource key, an
    endpoint the deployment doesn't live on, a misspelled deployment name, or an embedding
    width that doesn't match the index fails here instead of at first ingest.

    Every model / base_url / dimension probed comes from the catalog, and each entry is
    called by the client its api_style names, so the probe sends what the proxy will send.
    An entry whose bearer comes from the OAuth2 credential has no static key to call with,
    so the gateway's own probe stands in for it, and runs from here rather than waiting on
    a flag the operator may not pass.
    """
    gateway = get(inp, "inference.gateway")
    if gateway and not _gateway_probed:
        check_live_gateway(inp)
    section("Model endpoints (live)")
    targets, _source = _catalog_targets(inp)

    # The three chat tiers name one deployment; call it once.
    seen = set()
    deduped = []
    for surface, cid, entry in targets:
        key = (surface, entry.get("api_style"), entry.get("model"), entry.get("base_url"))
        if key in seen:
            continue
        seen.add(key)
        deduped.append((surface, cid, entry))

    routed, direct, pinecone = [], [], []
    for surface, cid, entry in deduped:
        style = entry.get("api_style")
        label = f"{surface} '{cid}'"
        # Dispatch on the credential, not the style: whatever its style, an entry drawing
        # its bearer from the OAuth2 credential has no static key to call with.
        if entry.get("credential_ref"):
            if _gateway_probed:
                ok(f"{label}: draws its bearer from the OAuth2 credential; the gateway "
                   "probe above is its call check")
            else:
                skip(f"{label}: draws its bearer from the OAuth2 credential, which only "
                     "--live-gateway mints, and no gateway probe ran")
        elif style == "litellm":
            routed.append((surface, cid, entry))
        elif style == "openai" and surface in OPENAI_PROBES:
            direct.append((surface, cid, entry))
        elif style == "pinecone" and surface in PINECONE_PROBES:
            pinecone.append((surface, cid, entry))
        else:
            warn(f"{label}: api_style {style!r} is not probed by --live-models, so nothing "
                 "here calls it")

    for surface, _cid, entry in direct:
        OPENAI_PROBES[surface](inp, entry)

    if pinecone:
        key = _pinecone_key()
        for surface, _cid, entry in (pinecone if key else []):
            PINECONE_PROBES[surface](inp, entry, key)

    if not routed:
        return
    lite = litellm_or_none()
    if lite is None:
        skip(f"litellm is not installed, so {len(routed)} litellm-style model(s) were not "
             f"called. It is the client the proxy uses for them. Re-run with: {litellm_hint()}")
        return
    for surface, _cid, entry in routed:
        PROBES[surface](inp, lite, entry)


def check_registry(inp):
    section("Image registry / pull secret")
    base = get(inp, "registry.base")
    server = get(inp, "registry.server")
    if not base:
        fail("registry.base is empty -> global.image.registry would be unset (images unresolved)")
        return
    ok(f"global.image.registry = {base}")
    if not server:
        fail("registry.server is empty (needed for the pull Secret docker-server)")
        return
    # The pull secret is keyed by registry host; it must serve the images the base points at.
    if base.split("/")[0] != server:
        fail(
            f"registry host mismatch: registry.base host '{base.split('/')[0]}' != "
            f"registry.server '{server}'. The pull Secret ({server}) would not cover "
            "the images pulled from the base."
        )
    else:
        ok(f"pull-secret server '{server}' matches the registry base host")
    if not get(inp, "registry.passwordEnv"):
        fail("registry.passwordEnv is empty (no env var named to source the pull password)")


def check_storage_auth(inp):
    section("Storage auth")
    auth = get(inp, "storage.auth", "shared_key")
    if auth not in ("shared_key", "workload_identity"):
        fail(f"storage.auth must be shared_key or workload_identity, got {auth!r}")
        return
    ok(f"storage.auth = {auth}")
    if auth == "workload_identity":
        if not get(inp, "storage.clientId"):
            fail("storage.auth=workload_identity requires storage.clientId (the UAMI client id)")
        else:
            ok("workload_identity clientId is set")
    else:
        if not get(inp, "storage.existingSecret"):
            fail("storage.auth=shared_key requires storage.existingSecret (the key Secret name)")
        else:
            ok(f"shared_key existingSecret = {get(inp, 'storage.existingSecret')}")
        if not get(inp, "storage.storageKeyEnv"):
            warn("storage.storageKeyEnv is empty — create-secrets.sh needs it to build the key Secret")


def check_security(inp):
    section("Security")
    if get(inp, "security.networkPolicyEnforcementCheck", True):
        ok("networkPolicy enforcement check enabled (a post-install hook fails the install "
           "if the cluster does not enforce the nexus-api NetworkPolicy)")
    else:
        warn("security.networkPolicyEnforcementCheck=false — the enforcement verification hook "
             "is skipped; ensure nexus-api is isolated at a lower layer")


def check_buckets_s3(inp):
    section("S3 buckets")
    prefix = get(inp, "storage.bucketPrefix")
    if not prefix:
        fail("storage.bucketPrefix is empty (blob.s3.bucketPrefix is required for s3)")
    if not get(inp, "storage.region"):
        fail("storage.region is empty (blob.s3.region is required — the AWS SDK has no default)")

    if prefix:
        names = [f"{prefix}-db"] + [f"{prefix}-nexus-{s}" for s in NEXUS_BUCKET_STORES]
        ok(f"7 buckets derive from stem '{prefix}': {', '.join(names)}")

    # Drift vs the emitted overlay, mirroring the abs container check.
    gs = load_gen("values.s3.yaml")
    gen_prefix = get(gs, "blob.s3.bucketPrefix") if gs else None
    if gen_prefix is not None and prefix and gen_prefix != prefix:
        fail(f"bucketPrefix drift: input '{prefix}' != generated blob.s3.bucketPrefix '{gen_prefix}'")


def check_storage_irsa(inp):
    section("S3 / IRSA")
    role = get(inp, "storage.roleArn", "")
    if not role:
        fail("storage.roleArn is empty (the IRSA role each blob-accessing SA assumes)")
    elif role.startswith("arn:aws:iam::") and ":role/" in role:
        ok(f"roleArn is an IAM role ARN ({role})")
    else:
        fail(f"storage.roleArn does not look like an IAM role ARN: {role!r}")


def check_buckets_gcs(inp):
    section("GCS buckets")
    prefix = get(inp, "storage.bucketPrefix")
    if not prefix:
        fail("storage.bucketPrefix is empty (blob.gcs.bucketPrefix is required for gcs)")

    if prefix:
        names = [f"{prefix}-db"] + [f"{prefix}-nexus-{s}" for s in NEXUS_BUCKET_STORES]
        ok(f"7 buckets derive from stem '{prefix}': {', '.join(names)}")

    # Drift vs the emitted overlay.
    gg = load_gen("values.gcs.yaml")
    gen_prefix = get(gg, "blob.gcs.bucketPrefix") if gg else None
    if gen_prefix is not None and prefix and gen_prefix != prefix:
        fail(f"bucketPrefix drift: input '{prefix}' != generated blob.gcs.bucketPrefix '{gen_prefix}'")


def check_storage_gcs(inp):
    section("GCS / Workload Identity")
    gsa = get(inp, "storage.serviceAccount", "")
    if not gsa:
        fail("storage.serviceAccount is empty (the GSA every blob-accessing SA impersonates)")
    elif gsa.endswith(".iam.gserviceaccount.com") and "@" in gsa:
        ok(f"serviceAccount is a GSA email ({gsa})")
    else:
        fail(f"storage.serviceAccount does not look like a GSA email: {gsa!r}")


# Fields the customer must fill with a value only they have; leftover example text
# here is exactly what slipped through on a real install and failed at curation. Rule 3
# (equals-example) is scoped to these [YOURS] fields so [DEFAULT]/[PINECONE] values that
# are meant to be kept as-is (staticIndex.id == bakedIndexId, host.name) never
# false-positive.
PLACEHOLDER_EXAMPLE_FIELDS = [
    "kubeContext",
    "registry.base",
    "registry.server",
    "registry.username",
    "storage.account",
    "storage.containerPrefix",
    "inference.endpoint",
    "inference.rerankEndpoint",
]


def _iter_strings(node, prefix=""):
    """Yield (dotted_path, value) for every string leaf, skipping the *Env fields — those
    hold env-var NAMES for secrets, and preflight must never treat a secret's name (or
    value) as config to scan."""
    if isinstance(node, dict):
        for k, v in node.items():
            if isinstance(k, str) and k.endswith("Env"):
                continue
            yield from _iter_strings(v, f"{prefix}.{k}" if prefix else k)
    elif isinstance(node, list):
        for i, v in enumerate(node):
            yield from _iter_strings(v, f"{prefix}[{i}]")
    elif isinstance(node, str):
        yield prefix, node


def _load_example():
    try:
        with open(os.path.join(HERE, "customer.example.yaml"), encoding="utf-8") as f:
            return yaml.safe_load(f) or {}
    except (OSError, yaml.YAMLError):
        return None


def check_placeholders(inp):
    section("Leftover example / placeholder values")
    # field -> (value, reason); one FAIL per field even if several rules match.
    offenders = {}

    for path, val in _iter_strings(inp):
        # Empty means not-yet-set-but-optional (e.g. ingress.host, host.url), not leftover.
        if not val.strip():
            continue
        if "acme" in val.lower():
            offenders.setdefault(path, (val, "contains the example org/stem 'acme'"))
        elif re.search(r"<[^>]+>", val):
            offenders.setdefault(path, (val, "has an unfilled <...> placeholder"))

    example = _load_example()
    if example is None:
        warn("could not read customer.example.yaml — skipping the equals-example comparison")
    else:
        for path in PLACEHOLDER_EXAMPLE_FIELDS:
            if path in offenders:
                continue
            val = get(inp, path)
            if isinstance(val, str) and val.strip() and val == get(example, path):
                offenders.setdefault(path, (val, "is unchanged from customer.example.yaml"))

    if offenders:
        for path in sorted(offenders):
            val, reason = offenders[path]
            fail(
                f"{path}={val!r} {reason} — looks like this value is still the example "
                "placeholder; edit customer.yaml before installing."
            )
    else:
        ok("no leftover example/placeholder values")


# ----------------------------------------------------------------------------- live
def check_live(inp):
    section("LIVE: kube context")
    ctx = get(inp, "kubeContext")
    rc, out = run(["kubectl", "--context", ctx, "version", "-o", "json"])
    if rc != 0:
        fail(f"kube context '{ctx}' not reachable: {out.splitlines()[0] if out else 'unknown error'}")
    else:
        ok(f"kube context '{ctx}' reachable")

    provider = storage_provider(inp)
    if provider == "s3":
        _live_s3_buckets(inp)
    elif provider == "gcs":
        _live_gcs_buckets(inp)
    else:
        _live_abs_containers(inp)

    section("LIVE: mirrored images")
    # tags come from the render (manifest.txt); the chart can bake a tag other than bundle.tag
    sub = get(inp, "azure.subscription", "")  # ACR tag-list check is Azure-only; absent on the S3/ECR path
    acr = get(inp, "registry.server", "")
    acr_name = acr.split(".")[0] if acr else ""
    is_acr = acr.endswith(".azurecr.io")
    manifest = os.path.join(_gen_dir, "manifest.txt")
    if not os.path.exists(manifest):
        warn(
            "no generated/manifest.txt — run `./image-manifest.sh --list --chart-path <chart>` "
            "first to record the exact refs the install pulls; skipping presence check"
        )
    elif not (sub and acr_name and is_acr):
        # a pull-through registry caches on first pull; there is no tag list to check
        warn(
            f"registry.server '{acr or '(unset)'}' is not an ACR (or azure.subscription unset) — "
            "cannot pre-verify presence for a pull-through/remote registry; images resolve "
            "lazily on first pull. Confirm the remote fronts the single upstream repo the "
            "bundle publishes from (image-manifest.sh lists it) so it resolves the whole bundle."
        )
    else:
        with open(manifest, encoding="utf-8") as f:
            refs = [ln.strip() for ln in f if ln.strip()]
        for ref in refs:
            body = ref.split("/", 1)[1]      # strip host -> <repo...>/<name>:<tag>
            repo, tag = body.rsplit(":", 1)
            _acr_tag_check(acr_name, sub, repo, tag)

    if provider == "s3":
        section("LIVE: IRSA trust")
        _check_irsa_trust(inp)
    elif provider == "gcs":
        section("LIVE: Workload Identity bindings")
        _check_wi_binding(inp)
    else:
        section("LIVE: workload identity federated credentials")
        auth = get(inp, "storage.auth", "shared_key")
        if auth != "workload_identity":
            ok("auth != workload_identity — no federated-credential coverage needed")
        else:
            _check_federation(inp)


def _live_abs_containers(inp):
    sub = get(inp, "azure.subscription")
    rg = get(inp, "azure.resourceGroup")
    acct = get(inp, "storage.account")
    prefix = get(inp, "storage.containerPrefix")

    section("LIVE: blob containers")
    if not (sub and rg and acct):
        warn("azure.subscription / azure.resourceGroup / storage.account incomplete — skipping")
        return
    rc, out = run([
        "az", "storage", "container-rm", "list", "--storage-account", acct,
        "-g", rg, "--subscription", sub, "--query", "[].name", "-o", "json",
    ])
    if rc != 0:
        warn(f"could not list containers: {out.splitlines()[0] if out else 'az error'}")
        return
    try:
        present = set(json.loads(out))
    except json.JSONDecodeError:
        present = set()
    for name in (f"{prefix}-{s}" for s in CONTAINER_SUFFIXES):
        (ok if name in present else fail)(
            f"container {name} {'present' if name in present else 'MISSING'}"
        )


def _live_s3_buckets(inp):
    section("LIVE: S3 buckets")
    prefix = get(inp, "storage.bucketPrefix")
    if not prefix:
        warn("no bucketPrefix to check — skipping")
        return
    names = [f"{prefix}-db"] + [f"{prefix}-nexus-{s}" for s in NEXUS_BUCKET_STORES]
    for name in names:
        rc, out = run(["aws", "s3api", "head-bucket", "--bucket", name])
        (ok if rc == 0 else fail)(
            f"bucket {name} {'present' if rc == 0 else 'MISSING or not accessible'}"
        )


def _check_irsa_trust(inp):
    # Pre-install analog of _check_federation: the IAM role terraform created must federate
    # every blob-accessing SA subject, or its pods get AccessDenied on S3. The chart annotates
    # the SAs from roleArn at install, so a covered trust policy is what makes those work.
    role_arn = get(inp, "storage.roleArn", "")
    ns = "nexus"
    if ":role/" not in role_arn:
        warn("storage.roleArn is not an IAM role ARN — skipping IRSA trust coverage")
        return
    role_name = role_arn.split(":role/", 1)[1]
    rc, out = run([
        "aws", "iam", "get-role", "--role-name", role_name,
        "--query", "Role.AssumeRolePolicyDocument", "--output", "json",
    ])
    if rc != 0:
        warn(f"could not read IAM role '{role_name}': {out.splitlines()[0] if out else 'aws error'}")
        return
    try:
        doc = json.loads(out)
    except json.JSONDecodeError:
        warn(f"could not parse the trust policy of '{role_name}'")
        return

    # Collect every federated subject the trust policy allows (the "<oidc>:sub" conditions).
    subjects = set()
    for stmt in doc.get("Statement", []):
        for op_values in (stmt.get("Condition") or {}).values():
            for key, val in op_values.items():
                if key.endswith(":sub"):
                    subjects.update(val if isinstance(val, list) else [val])

    missing = False
    for sa in BLOB_SERVICE_ACCOUNTS:
        subject = f"system:serviceaccount:{ns}:{sa}"
        if subject in subjects:
            ok(f"role trusts {sa}")
        else:
            fail(f"role '{role_name}' trust policy does not federate {sa} (subject '{subject}') — its pods will get AccessDenied on S3")
            missing = True
    if not missing:
        ok(f"all {len(BLOB_SERVICE_ACCOUNTS)} blob-accessing SAs federated on role '{role_name}'")


def _live_gcs_buckets(inp):
    section("LIVE: GCS buckets")
    prefix = get(inp, "storage.bucketPrefix")
    if not prefix:
        warn("no bucketPrefix to check — skipping")
        return
    names = [f"{prefix}-db"] + [f"{prefix}-nexus-{s}" for s in NEXUS_BUCKET_STORES]
    for name in names:
        rc, out = run(["gcloud", "storage", "buckets", "describe", f"gs://{name}", "--format=value(name)"])
        (ok if rc == 0 else fail)(
            f"bucket {name} {'present' if rc == 0 else 'MISSING or not accessible'}"
        )


def _check_wi_binding(inp):
    # The GSA must grant roles/iam.workloadIdentityUser to every blob-accessing KSA subject, or
    # its pods get 403 on GCS once the chart annotates the SAs from serviceAccount.
    gsa = get(inp, "storage.serviceAccount", "")
    project = get(inp, "storage.project", "")
    ns = "nexus"
    if "@" not in gsa or not gsa.endswith(".iam.gserviceaccount.com"):
        warn("storage.serviceAccount is not a GSA email — skipping Workload Identity binding coverage")
        return
    if not project:
        warn("storage.project is empty — cannot form the <project>.svc.id.goog members; skipping binding coverage")
        return

    pool = f"{project}.svc.id.goog"
    rc, out = run([
        "gcloud", "iam", "service-accounts", "get-iam-policy", gsa,
        "--project", project, "--format=json",
    ])
    if rc != 0:
        warn(f"could not read IAM policy of GSA '{gsa}': {out.splitlines()[0] if out else 'gcloud error'}")
        return
    try:
        doc = json.loads(out)
    except json.JSONDecodeError:
        warn(f"could not parse the IAM policy of '{gsa}'")
        return

    # Collect every member bound as roles/iam.workloadIdentityUser.
    members = set()
    for binding in doc.get("bindings", []):
        if binding.get("role") == "roles/iam.workloadIdentityUser":
            members.update(binding.get("members", []))

    missing = False
    for sa in BLOB_SERVICE_ACCOUNTS:
        member = f"serviceAccount:{pool}[{ns}/{sa}]"
        if member in members:
            ok(f"GSA binds {sa}")
        else:
            fail(f"GSA '{gsa}' has no workloadIdentityUser binding for {sa} (member '{member}') — its pods will get 403 on GCS")
            missing = True
    if not missing:
        ok(f"all {len(BLOB_SERVICE_ACCOUNTS)} blob-accessing SAs bound on GSA '{gsa}'")


def _acr_tag_check(acr_name, sub, repository, tag):
    rc, out = run([
        "az", "acr", "repository", "show-tags", "-n", acr_name, "--subscription", sub,
        "--repository", repository, "-o", "json",
    ])
    if rc != 0:
        warn(f"{repository}: could not query tags")
        return
    try:
        tags = set(json.loads(out))
    except json.JSONDecodeError:
        tags = set()
    (ok if tag in tags else fail)(
        f"{repository}:{tag} {'present' if tag in tags else 'MISSING from mirror'}"
    )


def _check_federation(inp):
    sub = get(inp, "azure.subscription")
    client_id = get(inp, "storage.clientId")
    ns = "nexus"
    if not (sub and client_id):
        warn("azure.subscription / storage.clientId incomplete — skipping")
        return

    # The inputs carry only the UAMI's client id; resolve its name + resource group from it.
    rc, out = run([
        "az", "identity", "list", "--subscription", sub,
        "--query", f"[?clientId=='{client_id}'].{{name:name, rg:resourceGroup}}", "-o", "json",
    ])
    ident = None
    if rc == 0 and out:
        try:
            hits = json.loads(out)
            ident = hits[0] if hits else None
        except json.JSONDecodeError:
            ident = None
    if not ident:
        warn(
            f"no managed identity with clientId {client_id} found in subscription {sub} "
            "(check az access / the id) — skipping federated-credential coverage"
        )
        return

    name, rg = ident["name"], ident["rg"]
    rc, out = run([
        "az", "identity", "federated-credential", "list",
        "--identity-name", name, "-g", rg, "--subscription", sub, "--query", "[].subject", "-o", "json",
    ])
    if rc != 0:
        warn(f"could not list federated credentials on '{name}': {out.splitlines()[0] if out else 'az error'}")
        return
    try:
        subjects = set(json.loads(out) or [])
    except json.JSONDecodeError:
        subjects = set()

    missing = False
    for sa in BLOB_SERVICE_ACCOUNTS:
        subject = f"system:serviceaccount:{ns}:{sa}"
        if subject in subjects:
            ok(f"federated credential covers {sa}")
        else:
            fail(f"no federated credential for {sa} (subject '{subject}') — its pods will 401 on blob")
            missing = True
    if not missing:
        ok(f"all {len(BLOB_SERVICE_ACCOUNTS)} blob-accessing SAs federated on UAMI '{name}'")


def main():
    ap = argparse.ArgumentParser(description="Preflight consistency validation for a Nexus install.")
    ap.add_argument("-f", "--inputs", default=os.path.join(HERE, "customer.yaml"))
    ap.add_argument("--gen-dir", default=os.path.join(HERE, "generated"),
                    help="dir with the generated overlays to cross-check (default: generated/)")
    ap.add_argument("--live", action="store_true", help="also run cloud/cluster checks (az/kubectl)")
    ap.add_argument("--live-gateway", action="store_true",
                    help="also mint a gateway token and make one real chat + embedding "
                         "(and rerank, when coversRerank) call (needs the client "
                         "id/secret env vars in this shell)")
    ap.add_argument("--only-live-gateway", action="store_true",
                    help="run ONLY the live gateway check, skipping every static check "
                         "(for proving gateway credentials from a bare host)")
    ap.add_argument("--live-models", action="store_true",
                    help="also make one real chat + embedding + rerank call straight at "
                         "the provider (needs the inference.*KeyEnv vars in this shell)")
    args = ap.parse_args()

    global _gen_dir
    _gen_dir = args.gen_dir

    if not os.path.exists(args.inputs):
        sys.stderr.write(f"preflight: inputs file not found: {args.inputs}\n")
        sys.exit(2)
    with open(args.inputs, encoding="utf-8") as f:
        inp = yaml.safe_load(f) or {}

    # --only-live-gateway skips the static suite so a tester can prove gateway
    # credentials on a bare host without a fully-filled customer.yaml.
    if args.only_live_gateway:
        print(f"Preflight: {args.inputs}  (live gateway only)")
        if args.live_models:
            warn("--only-live-gateway skips every other check, so --live-models did not run")
        check_live_gateway(inp)
    else:
        print(f"Preflight: {args.inputs}" + ("  (static + live)" if args.live else "  (static)"))
        check_dimension(inp)
        provider = storage_provider(inp)
        if provider == "s3":
            check_buckets_s3(inp)
        elif provider == "gcs":
            check_buckets_gcs(inp)
        else:
            check_containers(inp)
        check_inference(inp)
        catalog = _catalog_present(inp)
        if catalog:
            check_embedding_width(inp)
            check_catalog_structure(inp)
            check_model_registry(inp)
        check_registry(inp)
        if provider == "s3":
            check_storage_irsa(inp)
        elif provider == "gcs":
            check_storage_gcs(inp)
        else:
            check_storage_auth(inp)
        check_security(inp)
        check_placeholders(inp)
        if args.live:
            check_live(inp)
        if args.live_gateway:
            check_live_gateway(inp)
        if args.live_models and catalog:
            check_live_models(inp)

    print()
    skipped = f", {_skips} skipped" if _skips else ""
    if _fails:
        print(f"{RED}PREFLIGHT FAILED{RESET}: {_fails} error(s), {_warns} warning(s){skipped}.")
        sys.exit(1)
    print(f"{GREEN}PREFLIGHT PASSED{RESET}: 0 errors, {_warns} warning(s){skipped}.")
    if _skips:
        print(f"  {DIM}Some checks did not run. For the litellm-backed ones: "
              f"{litellm_hint()}{RESET}")


if __name__ == "__main__":
    main()
