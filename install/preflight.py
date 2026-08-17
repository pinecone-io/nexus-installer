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

LIVE checks (--live, opt-in, shells out to az/kubectl):
  - kube context reachable.
  - the seven blob containers exist.
  - every bundle image is present in the mirror at the expected tag.
  - the workload identity (resolved from its clientId) has a federated credential for
    each blob-accessing service account.

Exit 0 only if no check FAILs. WARN never fails the run.

Usage: python3 preflight.py [-f customer.yaml] [--live]
"""
import argparse
import json
import os
import re
import subprocess
import sys

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
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
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
    missing = refs - provider_keys
    if missing:
        fail(f"api_key_ref(s) without a providerKeys entry: {sorted(missing)}")
    else:
        ok(f"every catalog api_key_ref has a providerKeys entry ({sorted(refs)})")

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
    for field, name in {"embeddingDeployment": "text-embedding-3-small", "rerankDeployment": "rerank-v3.5"}.items():
        val = get(inp, f"inference.{field}")
        if val != name:
            warn(
                f"inference.{field}={val!r}: the router expects the deployment named "
                f"{name!r} (it matches on the deployment name)."
            )


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

    if storage_provider(inp) == "s3":
        _live_s3_buckets(inp)
    else:
        _live_abs_containers(inp)

    section("LIVE: mirrored images")
    # tags come from the render (manifest.txt); the chart can bake a tag other than bundle.tag
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

    if storage_provider(inp) == "s3":
        section("LIVE: IRSA")
        warn("SA role-arn annotations become checkable only after the chart is installed; "
             "re-run `preflight.py --live` against the running cluster to verify coverage.")
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
    args = ap.parse_args()

    global _gen_dir
    _gen_dir = args.gen_dir

    if not os.path.exists(args.inputs):
        sys.stderr.write(f"preflight: inputs file not found: {args.inputs}\n")
        sys.exit(2)
    with open(args.inputs, encoding="utf-8") as f:
        inp = yaml.safe_load(f) or {}

    print(f"Preflight: {args.inputs}" + ("  (static + live)" if args.live else "  (static)"))
    check_dimension(inp)
    check_embedding_width(inp)
    if storage_provider(inp) == "s3":
        check_buckets_s3(inp)
    else:
        check_containers(inp)
    check_inference(inp)
    check_registry(inp)
    if storage_provider(inp) == "s3":
        check_storage_irsa(inp)
    else:
        check_storage_auth(inp)
    check_placeholders(inp)
    if args.live:
        check_live(inp)

    print()
    if _fails:
        print(f"{RED}PREFLIGHT FAILED{RESET}: {_fails} error(s), {_warns} warning(s).")
        sys.exit(1)
    print(f"{GREEN}PREFLIGHT PASSED{RESET}: 0 errors, {_warns} warning(s).")


if __name__ == "__main__":
    main()
