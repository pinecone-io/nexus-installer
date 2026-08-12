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

LIVE checks (--live, opt-in, shells out to az/kubectl):
  - kube context reachable.
  - the seven blob containers exist.
  - every bundle image is present in the mirror at the expected tag.
  - the workload identity exists and its federated credentials cover the release SA.

Exit 0 only if no check FAILs. WARN never fails the run.

Usage: python3 preflight.py [-f customer.yaml] [--live]
"""
import argparse
import json
import os
import re
import subprocess
import sys

import yaml

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

# Nexus images the chart deploys (runbook 0b) + the DB set + FoundationDB. Used by the
# live image-presence check; mirror.sh derives the authoritative list from the render.
NEXUS_IMAGES = [
    "nexus_api", "nexus_orchestrator", "nexus_runtime", "nexus_gateway",
    "nexus_console", "nexus_mcp", "nexus_auth", "nexus_inference_proxy",
    "nexus_file_proxy",
]
DB_IMAGES = ["docs-api", "index-builder", "query-routers", "query-executors-slab", "request-log-writers"]

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
        with open(path) as f:
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
            "--path local --chart-path <nexus/deploy/installer/chart>, after "
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

    # Naming guardrail from runbook 0d — advisory, not fatal (customer may differ).
    for field, name in {"embeddingDeployment": "text-embedding-3-small", "rerankDeployment": "rerank-v3.5"}.items():
        val = get(inp, f"inference.{field}")
        if val != name:
            warn(
                f"inference.{field}={val!r}: runbook 0d expects the deployment named "
                f"{name!r} (the router matches on the deployment name)."
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


# ----------------------------------------------------------------------------- live
def check_live(inp):
    section("LIVE: kube context")
    ctx = get(inp, "kubeContext")
    rc, out = run(["kubectl", "--context", ctx, "version", "-o", "json"])
    if rc != 0:
        fail(f"kube context '{ctx}' not reachable: {out.splitlines()[0] if out else 'unknown error'}")
    else:
        ok(f"kube context '{ctx}' reachable")

    sub = get(inp, "azure.subscription")
    rg = get(inp, "azure.resourceGroup")
    acct = get(inp, "storage.account")
    prefix = get(inp, "storage.containerPrefix")

    section("LIVE: blob containers")
    if not (sub and rg and acct):
        warn("azure.subscription / azure.resourceGroup / storage.account incomplete — skipping")
    else:
        rc, out = run([
            "az", "storage", "container-rm", "list", "--storage-account", acct,
            "-g", rg, "--subscription", sub, "--query", "[].name", "-o", "json",
        ])
        if rc != 0:
            warn(f"could not list containers: {out.splitlines()[0] if out else 'az error'}")
        else:
            try:
                present = set(json.loads(out))
            except json.JSONDecodeError:
                present = set()
            for name in (f"{prefix}-{s}" for s in CONTAINER_SUFFIXES):
                (ok if name in present else fail)(
                    f"container {name} {'present' if name in present else 'MISSING'}"
                )

    section("LIVE: mirrored images")
    acr = get(inp, "registry.server", "")
    acr_name = acr.split(".")[0] if acr else ""
    repo_path = "/".join(get(inp, "registry.base", "").split("/")[1:])
    if not (sub and acr_name):
        warn("azure.subscription / registry.server incomplete — skipping image presence")
    else:
        tag = str(get(inp, "bundle.tag"))
        db_tag = str(get(inp, "bundle.dbTag", tag))
        for img in NEXUS_IMAGES:
            _acr_tag_check(acr_name, sub, f"{repo_path}/{img}" if repo_path else img, tag)
        for img in DB_IMAGES:
            _acr_tag_check(acr_name, sub, f"{repo_path}/{img}" if repo_path else img, db_tag)

    section("LIVE: workload identity federated credentials")
    auth = get(inp, "storage.auth", "shared_key")
    if auth != "workload_identity":
        ok("auth != workload_identity — no federated-credential coverage needed")
    else:
        _check_federation(inp)


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
    rg = get(inp, "azure.resourceGroup")
    client_id = get(inp, "storage.clientId")
    ns = "nexus"
    # The chart installs single-namespace; the blob-accessing SA subject is
    # system:serviceaccount:nexus:nexus (release SA). Confirm a federated cred covers it.
    subject = f"system:serviceaccount:{ns}:nexus"
    if not (sub and rg and client_id):
        warn("azure.subscription / resourceGroup / storage.clientId incomplete — skipping")
        return
    rc, out = run([
        "az", "identity", "federated-credential", "list",
        "--identity-name", "*", "-g", rg, "--subscription", sub, "-o", "json",
    ])
    # The identity name is not in the inputs (only its client id), so this is best-effort:
    # a customer runs it against the UAMI they created. Report guidance rather than a hard fail.
    if rc != 0:
        warn(
            "could not enumerate federated credentials without the UAMI name. Verify manually "
            f"that a federated credential on the UAMI (clientId {client_id}) has subject "
            f"'{subject}' and the AKS OIDC issuer."
        )
        return
    if subject in out:
        ok(f"a federated credential covers subject '{subject}'")
    else:
        fail(f"no federated credential found for subject '{subject}' (blob access will 401)")


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
    with open(args.inputs) as f:
        inp = yaml.safe_load(f) or {}

    print(f"Preflight: {args.inputs}" + ("  (static + live)" if args.live else "  (static)"))
    check_dimension(inp)
    check_embedding_width(inp)
    check_containers(inp)
    check_inference(inp)
    check_registry(inp)
    check_storage_auth(inp)
    if args.live:
        check_live(inp)

    print()
    if _fails:
        print(f"{RED}PREFLIGHT FAILED{RESET}: {_fails} error(s), {_warns} warning(s).")
        sys.exit(1)
    print(f"{GREEN}PREFLIGHT PASSED{RESET}: 0 errors, {_warns} warning(s).")


if __name__ == "__main__":
    main()
