#!/usr/bin/env python3
"""Generate the Helm values overlays for a Nexus self-hosted install from one inputs file.

Reads install/customer.yaml (the inputs contract, customer.example.yaml documents
every field) and emits, into the output dir (default install/generated/):

  values.install.yaml     top-level overrides the OCI chart's values.yaml cannot carry
                          (global.staticIndex, global.image/sizing,
                          ingress, nexus.config index/embed).
  values.abs.yaml         Azure Blob overlay: blob.* + global.blob.* + the db-slim
                          data-dir overlay + nexus.config.cloud=azure.
  values.self-hosted.yaml self-hosted config profile + the inference catalog + tiers +
                          empty providerKeys stubs (real keys are injected at install).
  inputs.env              non-secret scalars install.sh / image-manifest.sh / create-secrets.sh
                          need, so bash needs no YAML parser. Contains NO secrets.

Deterministic and secret-free: no key material is ever read or written here — the
catalog carries api_key_ref names only, and install.sh injects the values at install time.

Style follows the chart's gen-dbslim-values.py (python3 + PyYAML).

Usage: python3 gen-values.py [-f customer.yaml] [-o generated/]
"""
import argparse
import os
import shlex
import sys
import urllib.parse

try:
    import yaml
except ModuleNotFoundError:
    sys.exit(
        "PyYAML is required but not installed. From the install/ directory, run:\n"
        "  python3 -m venv .venv && source .venv/bin/activate && pip install -r requirements.txt"
    )

HERE = os.path.dirname(os.path.abspath(__file__))

# The seven container suffixes are a fixed product contract (see
# terraform/aks-slim/modules/storage-identity/main.tf). The stem supplies the rest.
CONTAINER_SUFFIXES = [
    "db",
    "nexus-source",
    "nexus-knowledge",
    "nexus-archive",
    "nexus-traces",
    "nexus-snapshots",
    "nexus-library",
]

# api_key_ref names the generated catalog uses; install.sh maps each to an env-held key.
LLM_KEY_REF = "llm-key"
EMBED_KEY_REF = "embedding-key"
RERANK_KEY_REF = "rerank-key"

# The closed set the proxy's model_family accepts; anything else fails its schema.
MODEL_FAMILIES = ("gpt5", "claude")

# The two rerank providers litellm routes that sign with cloud credentials rather than an
# API key -- bedrock via SigV4, vertex_ai via a Google access token. The proxy takes a
# model's credential from an env-held key or an OAuth2 client, so neither can be wired.
RERANK_PROVIDERS_WITHOUT_STATIC_KEY = ("bedrock", "vertex_ai")

DEFAULT_EMBEDDING_LIMITS = {"max_input_chars": 8000, "max_batch_size": 96}
DEFAULT_RERANK_LIMITS = {
    "max_query_chars": 1000, "max_doc_chars": 800, "max_docs_per_request": 100,
}

# Gateway posture (inference.gateway): the chat/embedding credential is a token the
# proxy mints per refresh window, so what install.sh injects is the long-lived OAuth2
# client plus the gateway's static subscription key.
GATEWAY_CREDENTIAL = "gateway"
GATEWAY_CLIENT_ID_REF = "gateway-client-id"
GATEWAY_CLIENT_SECRET_REF = "gateway-client-secret"
GATEWAY_SUBSCRIPTION_KEY_REF = "gateway-subscription-key"
DEFAULT_SUBSCRIPTION_HEADER = "Ocp-Apim-Subscription-Key"

SIZING_CLASSES = ("small",)


def die(msg):
    sys.stderr.write(f"gen-values: error: {msg}\n")
    sys.exit(1)


def req(d, path):
    """Fetch a required dotted key, failing with a clear message if absent/empty."""
    cur = d
    for part in path.split("."):
        if not isinstance(cur, dict) or part not in cur or cur[part] is None:
            die(f"missing required input `{path}`")
        cur = cur[part]
    if isinstance(cur, str) and cur.strip() == "":
        die(f"required input `{path}` is empty")
    return cur


def opt(d, path, default=None):
    cur = d
    for part in path.split("."):
        if not isinstance(cur, dict) or part not in cur or cur[part] is None:
            return default
        cur = cur[part]
    return cur


def int_opt(d, path, default):
    """An optional integer input, failing with a clear message on a non-numeric value."""
    val = opt(d, path, default)
    try:
        return int(val)
    except (TypeError, ValueError):
        die(f"input `{path}` must be an integer, got {val!r}")


def container_names(prefix):
    return [f"{prefix}-{s}" for s in CONTAINER_SUFFIXES]


def sizing(inp):
    s = str(opt(inp, "sizing", "small"))
    if s not in SIZING_CLASSES:
        die(f"sizing={s!r} is not a supported size class. "
            f"Supported: {', '.join(SIZING_CLASSES)}")
    return s


def storage_provider(inp):
    p = opt(inp, "storage.provider", "abs")
    if p not in ("abs", "s3", "gcs"):
        die(f"storage.provider must be abs, s3, or gcs, got {p!r}")
    return p


class BlockDumper(yaml.SafeDumper):
    # Expand every node inline (no anchors) so each emitted file is independently
    # editable — same rationale as gen-dbslim-values.py's NoAliasDumper.
    def ignore_aliases(self, data):
        return True


def dump(obj, path, header):
    with open(path, "w", encoding="utf-8") as f:
        f.write(header)
        yaml.dump(obj, f, Dumper=BlockDumper, default_flow_style=False, sort_keys=False, width=100)
    print(f"wrote {path}")


def _require_embedding_deployment(inp):
    value = _cfg(inp, "embedding", "deployment", "inference.embeddingDeployment")
    if not value:
        die("missing required input `inference.embedding.deployment` "
            "(or `inference.embeddingDeployment`)")
    return value


def _require_key_env(inp, block, flat):
    value = _cfg(inp, block, "keyEnv", flat)
    if not value:
        die(f"missing required input `inference.{block}.keyEnv` (or `{flat}`)")
    return value


def _tier_key_envs(inp):
    """{api_key_ref: env var name} for the chat tiers. A tier naming its own keyEnv gets
    its own ref, so a catalog whose tiers sit behind different credentials still resolves."""
    shared = _require_key_env(inp, "llm", "inference.llmKeyEnv")
    tiers = opt(inp, "inference.llm.tiers", {}) or {}
    out = {LLM_KEY_REF: shared}
    for tier in ("lite", "standard", "pro"):
        name = (tiers.get(tier) or {}).get("keyEnv", shared)
        out[LLM_KEY_REF if name == shared else f"{LLM_KEY_REF}-{tier}"] = name
    return out


def build_install_values(inp, dim):
    idx_id = req(inp, "staticIndex.id")
    image = {
        "registry": req(inp, "registry.base"),
        "pullSecrets": [{"name": opt(inp, "registry.pullSecretName", "acr-pull")}],
    }
    # An oci-stable-<id> is aliased onto every image, so pin the whole stack to it; a bare
    # sha keeps the chart's per-image pins.
    bundle_tag = str(req(inp, "bundle.tag"))
    if bundle_tag == "oci-stable-latest":
        die("bundle.tag oci-stable-latest is a moving tag (discovery only) — pin an immutable oci-stable-<id> for an install")
    if bundle_tag.startswith("oci-stable-"):
        image["tag"] = bundle_tag
    static_index = {
        "id": idx_id,
        "name": opt(inp, "staticIndex.name", "nexus-hybrid"),
        "dimension": dim,
    }
    values = {
        # Only global.* reaches the db-slim subchart, which renders its index env from
        # global.staticIndex; the chart requires it.
        "global": {"image": image, "sizing": sizing(inp), "staticIndex": static_index},
        "nexus": {
            "config": {
                "host": {"name": opt(inp, "host.name", "Nexus")},
                "indexMetadata": {"indexId": idx_id, "dimension": dim},
                "embeddingModel": {
                    "model": _require_embedding_deployment(inp),
                    "dimension": dim,
                },
            }
        },
    }

    host_url = opt(inp, "host.url", "")
    ingress_on = bool(opt(inp, "ingress.enabled", False))
    ingress_host = opt(inp, "ingress.host", "")
    if ingress_on:
        ing = {"enabled": True}
        if opt(inp, "ingress.className", ""):
            ing["className"] = inp["ingress"]["className"]
        if ingress_host:
            ing["host"] = ingress_host
        tls = opt(inp, "ingress.tlsSecretName", "")
        if tls:
            ing["tls"] = {"secretName": tls}
        values["ingress"] = ing
        if not host_url and ingress_host:
            host_url = f"https://{ingress_host}"
    if host_url:
        values["nexus"]["config"]["host"]["url"] = host_url

    return values


# The db-slim data-dir overlay: the base renders ssd-volume as an emptyDir, so --data-dir
# must sit at the mount root or the service ENOENTs at startup. Cloud-neutral, so both the
# abs and s3 overlays reuse it.
DBSLIM_DATA_DIR_OVERLAY = {
    "index-builder": {
        "pinecone": {
            "workload": {
                "args": [
                    "main",
                    "--data-dir=/cache",
                    "--port=10007",
                    "--scheduling=slab-based-indexing",
                    "--query_executor_slab_port=6012",
                ]
            }
        }
    },
    "executor-slab": {
        "pinecone": {
            "workload": {
                "command": [
                    "/opt/pinecone-db/query_executor_slab",
                    "--port=6012",
                    "--data-dir=/cache",
                ]
            }
        }
    },
}


def build_abs_values(inp):
    auth = opt(inp, "storage.auth", "shared_key")
    if auth not in ("shared_key", "workload_identity"):
        die(f"storage.auth must be shared_key or workload_identity, got {auth!r}")

    abs_block = {
        "account": req(inp, "storage.account"),
        "containerPrefix": req(inp, "storage.containerPrefix"),
        "secretKey": "azure-storage-access-key",
        "auth": auth,
    }
    if auth == "shared_key":
        abs_block["existingSecret"] = req(inp, "storage.existingSecret")
        abs_block["clientId"] = ""
    else:
        abs_block["existingSecret"] = opt(inp, "storage.existingSecret", "")
        abs_block["clientId"] = req(inp, "storage.clientId")

    blob = {"provider": "abs", "abs": dict(abs_block)}
    return {
        "blob": blob,
        # global.* is the only tree the db-slim + nexus subcharts read (see the chart
        # comment on blob.*); mirror it there too.
        "global": {"blob": {"provider": "abs", "abs": dict(abs_block)}},
        "db-slim": DBSLIM_DATA_DIR_OVERLAY,
        # pc-blob picks its driver from cloud.provider, not the blob backend.
        "nexus": {"config": {"cloud": {"provider": "azure"}, "storage": {"localRoot": ""}}},
    }


def build_s3_values(inp):
    region = req(inp, "storage.region")
    # The chart derives the seven bucket names from the stem and the IRSA SA annotation from
    # roleArn (charts/nexus/templates/_helpers.tpl), so nothing here is hand-listed per store.
    s3_block = {
        "bucketPrefix": req(inp, "storage.bucketPrefix"),
        "region": region,
        "roleArn": req(inp, "storage.roleArn"),
    }
    blob = {"provider": "s3", "s3": dict(s3_block)}
    return {
        "blob": blob,
        "global": {"blob": {"provider": "s3", "s3": dict(s3_block)}},
        "db-slim": DBSLIM_DATA_DIR_OVERLAY,
        "nexus": {"config": {"cloud": {"provider": "aws", "region": region}, "storage": {"localRoot": ""}}},
    }


def build_gcs_values(inp):
    # Requires a bundle whose chart schema accepts provider: gcs; an older chart rejects it at the
    # helm dry-run in install.sh (values.schema.json allows only local/abs/s3). The chart derives
    # the seven bucket names from the stem and the Workload-Identity SA annotation from
    # serviceAccount (charts/nexus/templates/_helpers.tpl), so nothing here is hand-listed per
    # store. No region: the GCS SDK addresses buckets by their global name.
    gcs_block = {
        "bucketPrefix": req(inp, "storage.bucketPrefix"),
        "serviceAccount": req(inp, "storage.serviceAccount"),
    }
    project = opt(inp, "storage.project", "")
    if project:
        gcs_block["project"] = project
    blob = {"provider": "gcs", "gcs": dict(gcs_block)}
    return {
        "blob": blob,
        "global": {"blob": {"provider": "gcs", "gcs": dict(gcs_block)}},
        "db-slim": DBSLIM_DATA_DIR_OVERLAY,
        "nexus": {"config": {"cloud": {"provider": "gcp"}, "storage": {"localRoot": ""}}},
    }


def _azure_ai_rerank_url(endpoint):
    # Pin /v2/rerank: litellm's azure_ai route defaults a bare base to the legacy
    # /v1/rerank (its cohere route doesn't), so `.../providers/cohere` would hit v1.
    base = endpoint.rstrip("/")
    if base.endswith("/v1/rerank") or base.endswith("/v2/rerank"):
        return base
    if base.endswith("/v1") or base.endswith("/v2"):
        return base + "/rerank"
    return base + "/v2/rerank"


def rerank_catalog_entry(provider, deployment, endpoint):
    """(model, base_url) for the rerank entry; base_url None lets litellm address the
    provider itself.

    Any provider litellm routes for rerank is accepted -- the proxy asks only that the id
    resolve to a rerank model and that the credential be one it can hold. azure_ai is the
    single shape needing help, because litellm sends its bare base to /v1/rerank.
    """
    if provider in RERANK_PROVIDERS_WITHOUT_STATIC_KEY:
        die(
            f"inference.rerankProvider={provider!r} signs its requests with cloud "
            "credentials rather than an API key, and a model's credential comes from an "
            "env-held key or an OAuth2 client. Pick a provider that issues an API key."
        )
    model = f"{provider}/{deployment}" if provider else deployment
    if provider == "azure_ai":
        if not endpoint:
            die(
                "inference.rerank.provider=azure_ai needs inference.rerank.endpoint: the "
                "deployment lives on a host of yours, which litellm cannot guess. Providers "
                "that publish one endpoint for everyone (cohere, voyage, jina_ai, ...) can "
                "leave it out."
            )
        return model, _azure_ai_rerank_url(endpoint)
    return model, (endpoint or None)


def gateway_spec(inp):
    """The inference.gateway block, or None for the direct-to-provider posture."""
    inference = inp.get("inference")
    if not isinstance(inference, dict) or "gateway" not in inference:
        return None
    if not inference["gateway"]:
        die(
            "inference.gateway is present but empty. Either fill it in (tokenUrl, "
            "clientIdEnv, clientSecretEnv, scope, apiVersion at minimum) or remove "
            "the `gateway:` line: "
            "an empty block would install the direct-to-provider catalog against what "
            "inference.endpoint now spells as a gateway base, and chat + embedding "
            "would fail at runtime. If you uncommented `gateway:` in customer.yaml, "
            "uncomment its fields too."
        )
    key_env = opt(inp, "inference.gateway.subscriptionKeyEnv", "")
    header = opt(inp, "inference.gateway.subscriptionHeader", None)
    if header and not key_env:
        die(
            "inference.gateway.subscriptionHeader is set but "
            "inference.gateway.subscriptionKeyEnv is not, so no subscription key would "
            "be sent and the gateway would reject every call. Set subscriptionKeyEnv to "
            "the env var holding the key, or drop subscriptionHeader if your gateway "
            "needs no key."
        )
    scope = str(opt(inp, "inference.gateway.scope", "")).strip()
    if not scope:
        die(
            "inference.gateway.scope is required. A client_credentials request that "
            "carries no scope is rejected by the authorization server (Okta answers "
            "HTTP 400 invalid_scope), so the proxy would never mint a token and every "
            "chat + embedding call would fail. Set it to the scope your gateway client "
            "is authorized for."
        )
    api_version = str(opt(inp, "inference.gateway.apiVersion", "")).strip()
    if not api_version:
        die(
            "inference.gateway.apiVersion is required. The gateway expects the "
            "?api-version= query param on every call and rejects a request without it, "
            "so chat + embedding would fail at runtime. Set it to the version the "
            "gateway publishes (e.g. 2025-01-01-preview)."
        )
    spec = {
        "token_url": req(inp, "inference.gateway.tokenUrl"),
        "client_id_env": req(inp, "inference.gateway.clientIdEnv"),
        "client_secret_env": req(inp, "inference.gateway.clientSecretEnv"),
        "scope": scope,
        "client_auth": opt(inp, "inference.gateway.clientAuth", "basic"),
        "subscription_key_env": key_env,
        "subscription_header": header or DEFAULT_SUBSCRIPTION_HEADER,
        "api_version": api_version,
        # Opt-in: rerank rides the minted gateway token, not a static key. Off by default
        # — a gateway may front only chat/embedding, not rerank.
        "covers_rerank": bool(opt(inp, "inference.gateway.coversRerank", False)),
    }
    if spec["client_auth"] not in ("basic", "post"):
        die(f"inference.gateway.clientAuth must be basic or post, got {spec['client_auth']!r}")
    if not spec["token_url"].startswith(("http://", "https://")):
        die("inference.gateway.tokenUrl must be an absolute http(s) URL")
    return spec


def cleartext_token_url_host(token_url):
    """The non-loopback host of an http:// token URL, else "" (nothing to warn about)."""
    if not token_url.startswith("http://"):
        return ""
    host = urllib.parse.urlsplit(token_url).hostname or ""
    if host in ("localhost", "127.0.0.1", "::1") or host.startswith("127."):
        return ""
    return host


def _gateway_model_extras(gw):
    """Fields every gateway-fronted model entry carries."""
    extras = {"credential_ref": GATEWAY_CREDENTIAL, "api_version": gw["api_version"]}
    if gw["subscription_key_env"]:
        extras["extra_header_refs"] = {
            gw["subscription_header"]: GATEWAY_SUBSCRIPTION_KEY_REF
        }
    return extras


def _gateway_rerank_extras(gw):
    """Like _gateway_model_extras but omits api_version — a litellm rerank model rejects it."""
    extras = {"credential_ref": GATEWAY_CREDENTIAL}
    if gw["subscription_key_env"]:
        extras["extra_header_refs"] = {
            gw["subscription_header"]: GATEWAY_SUBSCRIPTION_KEY_REF
        }
    return extras


def _limits(inp, path, defaults):
    """Per-surface ceilings: the defaults with any camelCase override from the inputs
    applied. Names differ between the two because the inputs are camelCase and the proxy's
    schema is snake_case."""
    out = dict(defaults)
    for field in defaults:
        camel = "".join(w if i == 0 else w.capitalize()
                        for i, w in enumerate(field.split("_")))
        value = opt(inp, f"{path}.{camel}", None)
        if value is not None:
            out[field] = int_opt(inp, f"{path}.{camel}", value)
    return out


def _cfg(inp, block, key, *flat, default=None):
    """One setting for one surface: the inference.<block> form wins, then any flat key it
    replaced, then the default. The flat keys stay live so an inputs file written against
    the earlier single-endpoint shape keeps generating the same catalog."""
    value = opt(inp, f"inference.{block}.{key}", None)
    if value is not None:
        return value
    for path in flat:
        value = opt(inp, path, None)
        if value is not None:
            return value
    return default


def _api_style(inp, block, gw, flat):
    style = _cfg(inp, block, "apiStyle", flat, "inference.apiStyle",
                 default="openai" if gw else "litellm")
    if style not in ("litellm", "openai"):
        die(f"inference.{block}.apiStyle must be 'litellm' or 'openai', got {style!r}")
    return style


def _model_family(inp, block, tier=None):
    where = f"inference.{block}.tiers.{tier}" if tier else f"inference.{block}"
    family = str(_cfg(inp, block, "modelFamily", "inference.modelFamily", default="") or "")
    if tier is not None:
        family = str(opt(inp, f"{where}.modelFamily", family) or "")
    family = family.strip()
    if family and family not in MODEL_FAMILIES:
        die(f"{where}.modelFamily must be one of {sorted(MODEL_FAMILIES)}, got {family!r}; "
            "the proxy's schema is a closed set")
    return family


def _surface_base_url(endpoint, deployment, gw):
    # A gateway publishes each deployment under its own path.
    if gw:
        return f"{endpoint.rstrip('/')}/deployments/{deployment}"
    return endpoint


def build_self_hosted_values(inp, dim):
    gw = gateway_spec(inp)

    llm_endpoint = _cfg(inp, "llm", "endpoint", "inference.chatBaseUrl", "inference.endpoint")
    embed_endpoint = _cfg(inp, "embedding", "endpoint", "inference.embeddingBaseUrl",
                          "inference.endpoint")
    if not llm_endpoint:
        die("missing required input `inference.llm.endpoint` (or `inference.endpoint`)")
    if not embed_endpoint:
        die("missing required input `inference.embedding.endpoint` (or `inference.endpoint`)")
    for name, value in (("llm", llm_endpoint), ("embedding", embed_endpoint)):
        lowered = str(value).lower()
        if gw and ("/deployments/" in lowered or lowered.rstrip("/").endswith("/deployments")):
            die(
                f"inference.{name} endpoint={value!r} already contains /deployments/. With a "
                "gateway configured it must be the base up to but NOT including "
                "/deployments/ — the generator appends /deployments/<deployment> itself, so "
                "this would produce a base_url the gateway answers with a 404. Trim "
                "everything from /deployments onward."
            )

    llm_provider = str(_cfg(inp, "llm", "provider", "inference.provider", default="azure") or "")
    llm_style = _api_style(inp, "llm", gw, "inference.chatApiStyle")
    llm_key_env = _cfg(inp, "llm", "keyEnv", "inference.llmKeyEnv")
    llm_deployment = _cfg(inp, "llm", "deployment", "inference.chatDeployment")
    if not llm_deployment:
        die("missing required input `inference.llm.deployment` (or `inference.chatDeployment`)")

    embed_provider = str(_cfg(inp, "embedding", "provider", "inference.provider",
                              default="azure") or "")
    embed_style = _api_style(inp, "embedding", gw, "inference.embeddingApiStyle")
    embed_deployment = _cfg(inp, "embedding", "deployment", "inference.embeddingDeployment")
    if not embed_deployment:
        die("missing required input `inference.embedding.deployment` "
            "(or `inference.embeddingDeployment`)")

    rerank_provider = _cfg(inp, "rerank", "provider", "inference.rerankProvider",
                           default="cohere")
    rerank_deployment = _cfg(inp, "rerank", "deployment", "inference.rerankDeployment")
    if not rerank_deployment:
        die("missing required input `inference.rerank.deployment` "
            "(or `inference.rerankDeployment`)")
    rerank_endpoint = str(_cfg(inp, "rerank", "endpoint", "inference.rerankEndpoint",
                               default="") or "").strip()
    rerank_model, rerank_base_url = rerank_catalog_entry(
        rerank_provider, rerank_deployment, rerank_endpoint)

    embedding_limits = _limits(inp, "inference.embedding.limits",
                               _limits(inp, "inference.embeddingLimits",
                                       DEFAULT_EMBEDDING_LIMITS))
    rerank_limits = _limits(inp, "inference.rerank.limits",
                            _limits(inp, "inference.rerankLimits", DEFAULT_RERANK_LIMITS))

    def model_id(provider, style, deployment):
        if style == "openai" or not provider:
            return deployment
        return f"{provider}/{deployment}"

    def budgets_for(style, tier=None):
        # api_style openai skips the registry default-fill, so its budgets must be stated.
        # Elsewhere a default would silently cap what the registry would have supplied.
        out = {}
        for field, key, flat, fallback in (
            ("context_window", "contextWindow", "inference.contextWindow", 272000),
            ("max_output_tokens", "maxOutputTokens", "inference.maxOutputTokens", 16384),
        ):
            value = _cfg(inp, "llm", key, flat)
            if tier is not None:
                value = opt(inp, f"inference.llm.tiers.{tier}.{key}", value)
            if value is not None:
                out[field] = int(value)
            elif style == "openai":
                out[field] = fallback
        return out

    tier_labels = {"lite": "Chat (lite)", "standard": "Chat (standard)", "pro": "Chat (pro)"}
    tiers_cfg = opt(inp, "inference.llm.tiers", {}) or {}
    unknown = sorted(set(tiers_cfg) - set(tier_labels))
    if unknown:
        die(f"inference.llm.tiers has unknown tier(s) {unknown}; the proxy's baseline is "
            f"{sorted(tier_labels)}")
    for tier, value in tiers_cfg.items():
        if not isinstance(value, dict):
            die(f"inference.llm.tiers.{tier} must be a mapping of the settings that tier "
                f"overrides, e.g. {{deployment: <model>}}; got {value!r}")

    auth = _gateway_model_extras(gw) if gw else {}
    llm_models, tier_key_envs = {}, {}
    for tier, label in tier_labels.items():
        per_tier = tiers_cfg.get(tier) or {}
        deployment = per_tier.get("deployment", llm_deployment)
        provider = str(per_tier.get("provider", llm_provider) or "")
        style = per_tier.get("apiStyle", llm_style)
        if style not in ("litellm", "openai"):
            die(f"inference.llm.tiers.{tier}.apiStyle must be 'litellm' or 'openai', "
                f"got {style!r}")
        if gw and style != "openai":
            die(
                f"inference.llm.tiers.{tier}.apiStyle={style!r} is not valid while "
                "inference.gateway is set. A gateway publishes each deployment under a "
                "path of its own, which the litellm route rewrites, so the call would "
                "not reach the gateway's route. Leave apiStyle unset on the tier so it "
                "uses the openai style the gateway path needs."
            )
        if gw and per_tier.get("keyEnv"):
            die(
                f"inference.llm.tiers.{tier}.keyEnv is set, but inference.gateway is "
                "configured and every chat tier authenticates with the gateway's OAuth2 "
                "credential. The per-tier key would be dropped and that tier would "
                "quietly share the gateway credential instead. Remove the tier's keyEnv, "
                "or remove the gateway block if this tier really has its own static key."
            )
        endpoint = per_tier.get("endpoint", llm_endpoint)
        family = _model_family(inp, "llm", tier)
        # Each tier gets its own key ref only when it names its own env var, so the common
        # case stays one injected key.
        key_env = per_tier.get("keyEnv", llm_key_env)
        key_ref = LLM_KEY_REF if key_env == llm_key_env else f"{LLM_KEY_REF}-{tier}"
        tier_key_envs[key_ref] = key_env
        llm_models[f"chat-{tier}"] = {
            "api_style": style,
            "model": model_id(provider, style, deployment),
            "base_url": _surface_base_url(endpoint, deployment, gw),
            **({} if gw else {"api_key_ref": key_ref}),
            "label": per_tier.get("label", label),
            "provider": _cfg(inp, "llm", "providerLabel", "inference.providerLabel",
                             default="gateway" if gw else (provider or "openai-compatible")),
            **({"model_family": family} if family else {}),
            **budgets_for(style, tier),
            **auth,
        }

    embed_entry = {
        "api_style": embed_style,
        "model": model_id(embed_provider, embed_style, embed_deployment),
        "base_url": _surface_base_url(embed_endpoint, embed_deployment, gw),
        **({} if gw else {"api_key_ref": EMBED_KEY_REF}),
        "dimension": dim,
        **embedding_limits,
        **auth,
    }
    # No default either way: off, a model that emits wider than `dimension` silently fills
    # the index with unusable vectors; on, a model that cannot narrow fails every call. Only
    # the operator knows which their model is, so make them say.
    request_dimensions = _cfg(inp, "embedding", "requestDimensions",
                              "embedding.requestDimensions")
    if request_dimensions is None:
        die(
            "embedding.requestDimensions must be set explicitly (true or false). true asks "
            "the model for `dimension`-wide vectors, for one that can narrow on request; "
            "false takes its native width, which `dimension` must then equal. Guessing "
            "either way risks an index full of wrong-width vectors."
        )
    if request_dimensions:
        embed_entry["request_dimensions"] = True
    embedding_models = {embed_deployment: embed_entry}

    # Gateway credential (auto-refreshed) when the gateway fronts rerank; else static key.
    rerank_gatewayed = bool(gw and gw["covers_rerank"])
    rerank_auth = _gateway_rerank_extras(gw) if rerank_gatewayed else {"api_key_ref": RERANK_KEY_REF}
    rerank_models = {
        "rerank": {
            "api_style": "litellm",
            "model": rerank_model,
            **({"base_url": rerank_base_url} if rerank_base_url else {}),
            **rerank_auth,
            **rerank_limits,
            # never set api_version on a litellm rerank model — the proxy rejects it.
        }
    }

    provider_keys = {}
    if not rerank_gatewayed:
        provider_keys[RERANK_KEY_REF] = ""
    credential_entry = None
    if gw:
        credential_entry = {
            "auth_style": "oauth2_client_credentials",
            "token_url": gw["token_url"],
            "client_id_ref": GATEWAY_CLIENT_ID_REF,
            "client_secret_ref": GATEWAY_CLIENT_SECRET_REF,
            "client_auth": gw["client_auth"],
            "scope": gw["scope"],
        }
        provider_keys[GATEWAY_CLIENT_ID_REF] = ""
        provider_keys[GATEWAY_CLIENT_SECRET_REF] = ""
        if gw["subscription_key_env"]:
            provider_keys[GATEWAY_SUBSCRIPTION_KEY_REF] = ""
        if rerank_gatewayed:
            sys.stderr.write(
                "gen-values: note: inference.gateway.coversRerank is on — rerank rides the "
                "gateway credential (minted, auto-refreshed token) and still POSTs to "
                "the rerank endpoint. Confirm the gateway actually fronts that rerank route.\n"
            )
        else:
            sys.stderr.write(
                "gen-values: note: inference.gateway applies to chat + embedding only; rerank "
                "uses inference.rerank.endpoint with its own key. Set inference.gateway.coversRerank: "
                "true to front rerank through the gateway too (token auto-refreshes like chat/embedding).\n"
            )
        cleartext_host = cleartext_token_url_host(gw["token_url"])
        if cleartext_host:
            sys.stderr.write(
                "gen-values: note: inference.gateway.tokenUrl is http:// (host "
                f"{cleartext_host}), so the OAuth2 client secret crosses the network in "
                "cleartext on every token refresh. Use https:// unless this is a local "
                "stand-in gateway.\n"
            )
    else:
        for key_ref in tier_key_envs:
            provider_keys[key_ref] = ""
        provider_keys[EMBED_KEY_REF] = ""

    # Without a cluster policy engine (AKS defaults to --network-policy none) the NetworkPolicy
    # is admitted but unenforced, so the verification hook fails the release; false skips it.
    np_enforcement_check = opt(inp, "security.networkPolicyEnforcementCheck", True)

    values = {
        "nexus": {
            "configProfiles": "self-hosted",
            "inference": {
                "llmModels": llm_models,
                "embeddingModels": embedding_models,
                "rerankModels": rerank_models,
                "tiers": {
                    "lite": "chat-lite",
                    "standard": "chat-standard",
                    "pro": "chat-pro",
                    "embedding": embed_deployment,
                    "rerank": "rerank",
                },
                # Empty stubs; real values are injected at install (never written here).
                "providerKeys": provider_keys,
            },
        }
    }
    if gw:
        values["nexus"]["inference"]["credentials"] = {
            GATEWAY_CREDENTIAL: credential_entry,
        }
    if not np_enforcement_check:
        values["nexus"]["networkPolicy"] = {"enforcementCheck": {"enabled": False}}
    return values


def build_inputs_env(inp, dim, outdir):
    """Non-secret scalars install.sh / image-manifest.sh / create-secrets.sh consume."""
    idx_id = req(inp, "staticIndex.id")
    provider = storage_provider(inp)
    for stale in ("bundle.bakedDimension", "bundle.bakedIndexId"):
        if opt(inp, stale) is not None:
            sys.stderr.write(
                f"gen-values: note: `{stale}` is no longer read — the index id and dimension "
                "come from staticIndex.id and embedding.dimension; remove it from customer.yaml.\n"
            )
    # Not required under coversRerank — rerank uses the gateway credential, not this key.
    gw = gateway_spec(inp)
    rerank_gatewayed = bool(gw and gw["covers_rerank"])
    env = {
        "KUBE_CONTEXT": req(inp, "kubeContext"),
        "REGISTRY_BASE": req(inp, "registry.base"),
        "REGISTRY_SERVER": req(inp, "registry.server"),
        "REGISTRY_USERNAME": req(inp, "registry.username"),
        "REGISTRY_PASSWORD_ENV": req(inp, "registry.passwordEnv"),
        "PULL_SECRET_NAME": opt(inp, "registry.pullSecretName", "acr-pull"),
        "BUNDLE_TAG": str(req(inp, "bundle.tag")),
        "CHART_VERSION": f"0.0.0-bundle.{req(inp, 'bundle.tag')}",
        "RERANK_KEY_ENV": (_cfg(inp, "rerank", "keyEnv", "inference.rerankKeyEnv", default="")
                           if rerank_gatewayed
                           else _require_key_env(inp, "rerank", "inference.rerankKeyEnv")),
        "RERANK_KEY_REF": RERANK_KEY_REF,
        "STATIC_INDEX_ID": idx_id,
        "EMBED_DIMENSION": str(dim),
        "NAMESPACE": "nexus",
        "RELEASE": "nexus",
    }

    # STORAGE_VALUES names the storage overlay install.sh feeds to helm; STORAGE_AUTH gates
    # whether create-secrets.sh makes a key Secret (only shared_key does — s3/IRSA is keyless).
    env["STORAGE_PROVIDER"] = provider
    if provider == "abs":
        env["STORAGE_VALUES"] = "values.abs.yaml"
        env["STORAGE_ACCOUNT"] = req(inp, "storage.account")
        env["CONTAINER_PREFIX"] = req(inp, "storage.containerPrefix")
        env["CONTAINER_NAMES"] = " ".join(container_names(req(inp, "storage.containerPrefix")))
        env["STORAGE_AUTH"] = opt(inp, "storage.auth", "shared_key")
        env["STORAGE_EXISTING_SECRET"] = opt(inp, "storage.existingSecret", "")
        env["STORAGE_KEY_ENV"] = opt(inp, "storage.storageKeyEnv", "")
        env["WI_CLIENT_ID"] = opt(inp, "storage.clientId", "")
    elif provider == "gcs":
        env["STORAGE_VALUES"] = "values.gcs.yaml"
        # Keyless: pods reach GCS via GKE Workload Identity, so no key Secret is created.
        env["STORAGE_AUTH"] = "workload_identity"
        env["STORAGE_EXISTING_SECRET"] = ""
        env["BUCKET_PREFIX"] = req(inp, "storage.bucketPrefix")
        env["GSA_EMAIL"] = req(inp, "storage.serviceAccount")
        env["GCP_PROJECT"] = opt(inp, "storage.project", "")
    else:
        env["STORAGE_VALUES"] = "values.s3.yaml"
        env["STORAGE_AUTH"] = "irsa"
        env["STORAGE_EXISTING_SECRET"] = ""
        env["BUCKET_PREFIX"] = req(inp, "storage.bucketPrefix")
        env["BLOB_REGION"] = req(inp, "storage.region")
        env["IRSA_ROLE_ARN"] = req(inp, "storage.roleArn")

    # Which credentials install.sh has to resolve depends on the posture: the gateway
    # one has no per-provider key at all, only the OAuth2 client and the gateway's
    # subscription key.
    env["GATEWAY_ENABLED"] = "1" if gw else "0"
    # coversRerank: tells install.sh to skip the static rerank key (catalog uses credential_ref).
    env["GATEWAY_COVERS_RERANK"] = "1" if rerank_gatewayed else "0"
    if gw:
        env["GATEWAY_CREDENTIAL"] = GATEWAY_CREDENTIAL
        env["GATEWAY_TOKEN_URL"] = gw["token_url"]
        env["GATEWAY_CLIENT_ID_ENV"] = gw["client_id_env"]
        env["GATEWAY_CLIENT_SECRET_ENV"] = gw["client_secret_env"]
        env["GATEWAY_CLIENT_ID_REF"] = GATEWAY_CLIENT_ID_REF
        env["GATEWAY_CLIENT_SECRET_REF"] = GATEWAY_CLIENT_SECRET_REF
        env["GATEWAY_SUBSCRIPTION_KEY_ENV"] = gw["subscription_key_env"]
        env["GATEWAY_SUBSCRIPTION_KEY_REF"] = (
            GATEWAY_SUBSCRIPTION_KEY_REF if gw["subscription_key_env"] else ""
        )
    else:
        env["LLM_KEY_ENV"] = _require_key_env(inp, "llm", "inference.llmKeyEnv")
        env["EMBEDDING_KEY_ENV"] = _require_key_env(inp, "embedding",
                                                    "inference.embeddingKeyEnv")
        env["LLM_KEY_REF"] = LLM_KEY_REF
        env["EMBED_KEY_REF"] = EMBED_KEY_REF
        extra = " ".join(f"{ref}:{name}" for ref, name in sorted(_tier_key_envs(inp).items())
                         if ref != LLM_KEY_REF)
        env["EXTRA_KEY_PAIRS"] = extra

    path = os.path.join(outdir, "inputs.env")
    with open(path, "w", encoding="utf-8") as f:
        f.write("# GENERATED by gen-values.py — non-secret scalars for the shell tools.\n")
        f.write("# Secrets are NOT here; they are read from the env vars named by *_ENV.\n")
        for k, v in env.items():
            f.write(f"{k}={shlex.quote(str(v))}\n")
    print(f"wrote {path}")


def main():
    ap = argparse.ArgumentParser(description="Generate Helm values overlays from the inputs file.")
    ap.add_argument("-f", "--inputs", default=os.path.join(HERE, "customer.yaml"))
    ap.add_argument("-o", "--outdir", default=os.path.join(HERE, "generated"))
    args = ap.parse_args()

    if not os.path.exists(args.inputs):
        die(f"inputs file not found: {args.inputs} (copy customer.example.yaml to customer.yaml)")
    with open(args.inputs, encoding="utf-8") as f:
        inp = yaml.safe_load(f) or {}

    dim = int(req(inp, "embedding.dimension"))
    os.makedirs(args.outdir, exist_ok=True)

    dump(
        build_install_values(inp, dim),
        os.path.join(args.outdir, "values.install.yaml"),
        "# GENERATED by gen-values.py — top-level overrides the OCI chart's values.yaml cannot\n"
        "# carry (global.staticIndex, global.image/sizing, ingress,\n"
        "# nexus.config index/embed).\n",
    )
    provider = storage_provider(inp)
    if provider == "s3":
        dump(
            build_s3_values(inp),
            os.path.join(args.outdir, "values.s3.yaml"),
            "# GENERATED by gen-values.py — AWS S3 overlay (blob.* + global.blob.* +\n"
            "# db-slim data-dir + nexus cloud=aws + IRSA SA annotation).\n",
        )
    elif provider == "gcs":
        dump(
            build_gcs_values(inp),
            os.path.join(args.outdir, "values.gcs.yaml"),
            "# GENERATED by gen-values.py — GCS overlay (blob.* + global.blob.* +\n"
            "# db-slim data-dir + nexus cloud=gcp + Workload-Identity SA annotation).\n",
        )
    else:
        dump(
            build_abs_values(inp),
            os.path.join(args.outdir, "values.abs.yaml"),
            "# GENERATED by gen-values.py — Azure Blob overlay (blob.* + global.blob.* +\n"
            "# db-slim data-dir + nexus cloud=azure).\n",
        )
    dump(
        build_self_hosted_values(inp, dim),
        os.path.join(args.outdir, "values.self-hosted.yaml"),
        "# GENERATED by gen-values.py — self-hosted profile + inference catalog.\n"
        "# providerKeys are empty stubs; real keys are injected at install time.\n",
    )
    build_inputs_env(inp, dim, args.outdir)


if __name__ == "__main__":
    main()
