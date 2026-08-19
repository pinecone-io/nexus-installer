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
  - image registry override set; pull-secret server is a prefix of the registry base.
  - workload_identity: clientId set. shared_key: existingSecret set.
  - no leftover example/placeholder values (an `acme` token, an unfilled <...>, or a
    [YOURS] field still equal to customer.example.yaml).

LIVE gateway check (--live-gateway, opt-in, makes real HTTP calls):
  - mints an OAuth2 client_credentials token, then makes one 1-token chat completion
    and one tiny embedding call through the gateway — bodies shaped the way the
    inference proxy shapes them — so a wrong client secret / scope / gateway
    environment, a missing embeddings route, or a gateway that drops the
    `dimensions` request fails here in seconds instead of as 401s after the install.
  - --only-live-gateway runs this check and nothing else, so it needs only the
    inference/embedding inputs, only the standard library, and can be handed to
    whoever holds the credentials.

LIVE checks (--live, opt-in, shells out to az/kubectl):
  - kube context reachable.
  - the seven blob containers exist.
  - every bundle image is present in the mirror at the expected tag.
  - the workload identity (resolved from its clientId) has a federated credential for
    each blob-accessing service account.

Exit 0 only if no check FAILs. WARN never fails the run.

Usage: python3 preflight.py [-f customer.yaml] [--live] [--live-gateway | --only-live-gateway]
"""
import argparse
import base64
import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request

PYYAML_HINT = (
    "PyYAML is not installed. The gateway credential check alone runs without it:\n"
    "  python3 preflight.py -f customer.yaml --only-live-gateway\n"
    "For the full preflight, from the install/ directory:\n"
    "  python3 -m venv .venv && source .venv/bin/activate && pip install -r requirements.txt"
)

try:
    import yaml
except ModuleNotFoundError:
    yaml = None


class _YamlSubset:
    """A stand-in for yaml.safe_load over nested maps of scalars. It backs
    --only-live-gateway alone, so that check runs on a machine that has the gateway
    credentials and nothing else — every other mode still requires PyYAML, and anything
    this cannot read raises rather than yield a half-parsed inputs file.
    """

    class YAMLError(Exception):
        pass

    KEY = re.compile(r"^([A-Za-z0-9_.\-]+):(.*)$")
    WORDS = {"true": True, "yes": True, "on": True, "false": False, "no": False,
             "off": False, "null": None, "~": None, "": None}

    @classmethod
    def safe_load(cls, stream):
        text = stream.read() if hasattr(stream, "read") else stream
        entries = []
        for n, raw in enumerate(text.splitlines(), 1):
            line = raw.rstrip()
            if not line.strip() or line.lstrip().startswith("#"):
                continue
            indent = len(line) - len(line.lstrip(" "))
            m = cls.KEY.match(line.strip())
            value = re.split(r"\s+#", m.group(2), maxsplit=1)[0].strip() if m else ""
            if "\t" in raw or not m or value[:1] in ("{", "[", "|", ">", "&", "*", "!"):
                raise cls.YAMLError(f"line {n} ({line.strip()[:40]!r}) is beyond this "
                                    f"reader. {PYYAML_HINT}")
            entries.append((indent, m.group(1), value))

        root = {}
        stack = [(-1, root)]
        for i, (indent, key, value) in enumerate(entries):
            while indent <= stack[-1][0]:
                stack.pop()
            nested = value == "" and i + 1 < len(entries) and entries[i + 1][0] > indent
            stack[-1][1][key] = {} if nested else cls._scalar(value)
            if nested:
                stack.append((indent, stack[-1][1][key]))
        return root

    @classmethod
    def _scalar(cls, value):
        if len(value) > 1 and value[0] == value[-1] and value[0] in "\"'":
            return value[1:-1]
        if value.lower() in cls.WORDS:
            return cls.WORDS[value.lower()]
        try:
            return int(value)
        except ValueError:
            pass
        try:
            return float(value)
        except ValueError:
            return value


if yaml is None:
    yaml = _YamlSubset

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

GREEN, RED, YELLOW, RESET = "\033[32m", "\033[31m", "\033[33m", "\033[0m"
if not sys.stdout.isatty():
    GREEN = RED = YELLOW = RESET = ""

_fails = 0
_warns = 0

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


# --------------------------------------------------------------------------- static
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

    # Cross-check the emitted overlay: the three dimension sites must all equal `dim`.
    gi = load_gen("values.install.yaml")
    if gi:
        sites = {
            "staticIndex.dimension": get(gi, "staticIndex.dimension"),
            "nexus.config.indexMetadata.dimension": get(gi, "nexus.config.indexMetadata.dimension"),
            "nexus.config.embeddingModel.dimension": get(gi, "nexus.config.embeddingModel.dimension"),
        }
        bad = {k: v for k, v in sites.items() if v is not None and int(v) != int(dim)}
        if bad:
            fail(f"dimension drift in generated values.install.yaml vs embedding.dimension={dim}: {bad}")
        else:
            ok(f"generated overlay: all dimension sites == {dim}")
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


# Native output widths of common embedding models, keyed by a substring of the
# deployment name. Used to catch the silent case where a model emits a width the
# index does not expect. Matryoshka models (text-embedding-3-*) can be reduced via
# request_dimensions; others cannot.
KNOWN_NATIVE_DIMS = {
    "text-embedding-3-small": (1536, True),
    "text-embedding-3-large": (3072, True),
    "text-embedding-ada-002": (1536, False),
    "multilingual-e5-large": (1024, False),
}


def _embedding_facts(inp):
    """Effective (model_name, declared_dim, request_dimensions) from the emitted
    self-hosted overlay when present (the artifact that ships), else from inputs with
    the same Matryoshka auto-default the generator applies."""
    gsh = load_gen("values.self-hosted.yaml")
    ems = get(gsh, "nexus.inference.embeddingModels") if gsh else None
    if ems:
        tiers = get(gsh, "nexus.inference.tiers") or {}
        key = tiers.get("embedding") or next(iter(ems))
        e = ems.get(key, {}) or {}
        model = str(e.get("model", "")).split("/")[-1]
        return model, e.get("dimension"), bool(e.get("request_dimensions", False))
    model = get(inp, "inference.embeddingDeployment") or ""
    req = get(inp, "embedding.requestDimensions", None)
    if req is None:
        req = model.lower().startswith("text-embedding-3")
    return model, get(inp, "embedding.dimension"), bool(req)


def check_embedding_width(inp):
    section("Embedding model output width")
    model, dim, req = _embedding_facts(inp)
    model = model.lower()
    baked = get(inp, "bundle.bakedDimension")
    if dim is None or not model:
        return
    native = matryoshka = None
    for name, (width, matr) in KNOWN_NATIVE_DIMS.items():
        if name in model:
            native, matryoshka = width, matr
            break
    if native is None:
        warn(f"unknown embedding model '{model}' — can't verify its native width equals {dim}")
        return
    if native == int(dim):
        ok(f"'{model}' native width {native} == target dimension {dim} (no reduction needed)")
        return
    # target != native: the model must be asked to reduce, and must be able to.
    if matryoshka and req:
        ok(
            f"'{model}' native {native} -> request_dimensions asks for {dim} (Matryoshka; "
            "needs a bundle whose proxy honors the dimensions request)"
        )
    elif matryoshka and not req:
        fail(
            f"'{model}' emits {native}-wide vectors natively but the declared dimension is {dim}. "
            "Set embedding.requestDimensions: true so the model truncates to the declared width "
            "(Matryoshka; needs a bundle whose proxy honors it). Otherwise the vectors "
            "won't match the index and ingest fails."
        )
    else:
        # Non-Matryoshka model that can't reach the declared width by reduction.
        via = ""
        if baked is not None and int(native) != int(baked):
            via = (f" Since {native} != the baked default {baked}, that is the local-chart path "
                   "(remint-dbslim.sh + install.sh --path local).")
        fail(
            f"'{model}' is not reducible, so it can't emit the declared dimension {dim} "
            f"(it outputs {native}). Set the dimension to {native} or choose a Matryoshka model "
            f"(text-embedding-3-*).{via}"
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
            "llmModels": {"chat": {"api_key_ref": "llm-key"}},
            "embeddingModels": {embed: {"api_key_ref": "embedding-key"}},
            "rerankModels": {"rerank": {"api_key_ref": "rerank-key"}},
            "tiers": {"lite": "chat", "standard": "chat", "pro": "chat", "embedding": embed, "rerank": "rerank"},
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
    chat_tiers = [tiers.get(s) for s in ("lite", "standard", "pro")]
    if all(chat_tiers):
        ok("three chat tier slots configured (lite/standard/pro)")
    else:
        fail(f"chat tiers incomplete — lite/standard/pro must all be set, got {chat_tiers}")

    # Naming guardrail — advisory, not fatal (customer may differ).
    embed_dep = get(inp, "inference.embeddingDeployment")
    if embed_dep != "text-embedding-3-small":
        warn(
            f"inference.embeddingDeployment={embed_dep!r}: the recommended model is "
            "'text-embedding-3-small'."
        )
    # Rerank: the proxy validates `<rerankProvider>/<rerankDeployment>` against LiteLLM's
    # registry at startup, so flag combos LiteLLM likely won't map (it's not a fixed name).
    rerank_dep = get(inp, "inference.rerankDeployment")
    rerank_provider = get(inp, "inference.rerankProvider", "cohere")
    if rerank_provider not in ("cohere", "azure_ai"):
        fail(f"inference.rerankProvider must be 'cohere' or 'azure_ai', got {rerank_provider!r}")
    elif rerank_provider == "cohere" and rerank_dep and not rerank_dep.startswith("rerank-v3"):
        warn(
            f"rerankProvider=cohere + rerankDeployment={rerank_dep!r} → model 'cohere/{rerank_dep}', "
            "which LiteLLM may not map (proxy fails to start if not). Known-good: 'rerank-v3.5'; "
            "for a newer reranker use rerankProvider=azure_ai (e.g. cohere-rerank-v4.0-fast)."
        )
    elif rerank_provider == "azure_ai" and rerank_dep and not rerank_dep.startswith("cohere-rerank-"):
        warn(
            f"rerankProvider=azure_ai + rerankDeployment={rerank_dep!r} → model 'azure_ai/{rerank_dep}'; "
            "azure_ai expects LiteLLM's canonical name (e.g. cohere-rerank-v4.0-fast), which must "
            "also be your Foundry deployment name."
        )


def _is_gpt5_family(model):
    """The inference proxy's own gpt-5 test; it renames max_tokens to
    max_completion_tokens for exactly these model ids, so the probe must too."""
    m = (model or "").lower()
    return m.startswith("gpt-5") or "/gpt-5" in m or "gpt-5." in m


def _request_dimensions(inp, embed):
    """Whether the generated catalog sets request_dimensions (same rule as gen-values)."""
    explicit = get(inp, "embedding.requestDimensions")
    if explicit is None:
        return str(embed).lower().startswith("text-embedding-3")
    return bool(explicit)


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
    budget = "max_completion_tokens" if _is_gpt5_family(chat) else "max_tokens"
    chat_url = f"{endpoint}/deployments/{chat}/chat/completions{query}"
    payload = json.dumps(
        {"model": chat, "messages": [{"role": "user", "content": "ping"}], budget: 1}
    ).encode()
    status, body = _http_post(chat_url, payload, call_headers)
    if not _gateway_call_failed(chat_url, status, body):
        ok(f"chat completion through the gateway succeeded ({chat_url}, {budget})")

    embed_url = f"{endpoint}/deployments/{embed}/embeddings{query}"
    embed_body = {"model": embed, "input": "ping"}
    want_dim = get(inp, "embedding.dimension") if _request_dimensions(inp, embed) else None
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
            "would be the wrong width for the index. Either the gateway strips the "
            "field or the deployment is not Matryoshka-capable — set "
            "embedding.requestDimensions: false and embedding.dimension to the width "
            "the model actually emits."
        )


def _http_post(url, data, headers, timeout=20):
    request = urllib.request.Request(url, data=data, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, response.read().decode(errors="replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode(errors="replace")
    except Exception as e:
        return 0, f"{type(e).__name__}: {e}"


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
                         "call (needs the client id/secret env vars in this shell)")
    ap.add_argument("--only-live-gateway", action="store_true",
                    help="run the gateway check alone — no static checks, no overlays — for "
                         "an inputs file that carries only the inference/embedding values")
    args = ap.parse_args()
    if args.only_live_gateway:
        args.live_gateway = True
    elif yaml is _YamlSubset:
        sys.exit(PYYAML_HINT)

    global _gen_dir
    _gen_dir = args.gen_dir

    if not os.path.exists(args.inputs):
        sys.stderr.write(f"preflight: inputs file not found: {args.inputs}\n")
        sys.exit(2)
    with open(args.inputs, encoding="utf-8") as f:
        try:
            inp = yaml.safe_load(f) or {}
        except yaml.YAMLError as e:
            sys.stderr.write(f"preflight: could not read {args.inputs}: {e}\n")
            sys.exit(2)

    if args.only_live_gateway:
        scope = "  (live gateway only)"
    else:
        scope = "  (static + live)" if args.live else "  (static)"
    print(f"Preflight: {args.inputs}{scope}")
    if not args.only_live_gateway:
        check_dimension(inp)
        check_embedding_width(inp)
        provider = storage_provider(inp)
        if provider == "s3":
            check_buckets_s3(inp)
        elif provider == "gcs":
            check_buckets_gcs(inp)
        else:
            check_containers(inp)
        check_inference(inp)
        check_registry(inp)
        if provider == "s3":
            check_storage_irsa(inp)
        elif provider == "gcs":
            check_storage_gcs(inp)
        else:
            check_storage_auth(inp)
        check_placeholders(inp)
        if args.live:
            check_live(inp)
    if args.live_gateway:
        check_live_gateway(inp)

    print()
    if _fails:
        print(f"{RED}PREFLIGHT FAILED{RESET}: {_fails} error(s), {_warns} warning(s).")
        sys.exit(1)
    print(f"{GREEN}PREFLIGHT PASSED{RESET}: 0 errors, {_warns} warning(s).")


if __name__ == "__main__":
    main()
