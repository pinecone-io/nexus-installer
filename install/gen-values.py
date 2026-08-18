#!/usr/bin/env python3
"""Generate the Helm values overlays for a Nexus self-hosted install from one inputs file.

Reads install/customer.yaml (the inputs contract, customer.example.yaml documents
every field) and emits, into the output dir (default install/generated/):

  values.install.yaml     top-level overrides the OCI chart's values.yaml cannot carry
                          (staticIndex, global.image, ingress, nexus.config index/embed).
  values.abs.yaml         Azure Blob overlay: blob.* + global.blob.* + the db-slim
                          data-dir overlay + nexus.config.cloud=azure.
  values.self-hosted.yaml self-hosted config profile + the inference catalog + tiers +
                          empty providerKeys stubs (real keys are --set at install).
  inputs.env              non-secret scalars install.sh / image-manifest.sh / create-secrets.sh
                          need, so bash needs no YAML parser. Contains NO secrets.

Deterministic and secret-free: no key material is ever read or written here — the
catalog carries api_key_ref names only, and install.sh injects the values via --set.

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

# Gateway posture (inference.gateway): the chat/embedding credential is a token the
# proxy mints per refresh window, so what install.sh injects is the long-lived OAuth2
# client plus the gateway's static subscription key.
GATEWAY_CREDENTIAL = "gateway"
GATEWAY_CLIENT_ID_REF = "gateway-client-id"
GATEWAY_CLIENT_SECRET_REF = "gateway-client-secret"
GATEWAY_SUBSCRIPTION_KEY_REF = "gateway-subscription-key"
DEFAULT_SUBSCRIPTION_HEADER = "Ocp-Apim-Subscription-Key"


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


def container_names(prefix):
    return [f"{prefix}-{s}" for s in CONTAINER_SUFFIXES]



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
    values = {
        "staticIndex": {
            "id": idx_id,
            "name": opt(inp, "staticIndex.name", "nexus-hybrid"),
            "dimension": dim,
        },
        "global": {"image": image},
        "nexus": {
            "config": {
                "host": {"name": opt(inp, "host.name", "Nexus")},
                "indexMetadata": {"indexId": idx_id, "dimension": dim},
                "embeddingModel": {
                    "model": req(inp, "inference.embeddingDeployment"),
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
    # (model, base_url) for the rerank entry. The proxy validates the model id at startup
    # (litellm.get_model_info): `cohere/` only maps rerank-v3.5, so `azure_ai/` routes a
    # newer reranker under litellm's canonical name. See customer.example.yaml for the
    # rerankProvider/rerankDeployment naming rules.
    if provider == "cohere":
        return f"cohere/{deployment}", endpoint
    if provider == "azure_ai":
        return f"azure_ai/{deployment}", _azure_ai_rerank_url(endpoint)
    die(f"inference.rerankProvider must be 'cohere' or 'azure_ai', got {provider!r}")


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


def build_self_hosted_values(inp, dim):
    endpoint = req(inp, "inference.endpoint")
    rerank_endpoint = req(inp, "inference.rerankEndpoint")
    chat = req(inp, "inference.chatDeployment")
    embed = req(inp, "inference.embeddingDeployment")
    rerank = req(inp, "inference.rerankDeployment")
    # Fallback-when-omitted stays cohere so a pre-existing customer.yaml (rerank-v3.5, no
    # rerankProvider) is unchanged; customer.example.yaml recommends azure_ai for new installs.
    rerank_provider = opt(inp, "inference.rerankProvider", "cohere")
    rerank_model, rerank_base_url = rerank_catalog_entry(rerank_provider, rerank, rerank_endpoint)

    gw = gateway_spec(inp)
    if gw and ("/deployments/" in endpoint or endpoint.rstrip("/").endswith("/deployments")):
        die(
            f"inference.endpoint={endpoint!r} already contains /deployments/. With a "
            "gateway configured it must be the gateway base up to but NOT including "
            "/deployments/ — the generator appends /deployments/<deployment> itself, so "
            f"this would produce base_url {endpoint.rstrip('/')}/deployments/{chat}, "
            "which the gateway answers with a 404 at runtime. Trim everything from "
            "/deployments onward."
        )

    # the proxy requires each tier to resolve to a distinct model ref
    tier_labels = {"lite": "Chat (lite)", "standard": "Chat (standard)", "pro": "Chat (pro)"}
    if gw:
        # api_style openai sends the path exactly as base_url spells it. litellm's
        # `azure/` provider would insert /openai/deployments/, which an APIM front
        # door does not serve, and its registry would reject a deployment name it
        # does not know.
        extras = _gateway_model_extras(gw)
        # api_style openai does no model-registry lookup, so the token budgets the
        # proxy would otherwise introspect have to be stated. They are properties of
        # the deployment behind the gateway, which only the customer knows.
        context_window = int(opt(inp, "inference.contextWindow", 272000))
        max_output_tokens = int(opt(inp, "inference.maxOutputTokens", 16384))
        llm_models = {
            f"chat-{t}": {
                "api_style": "openai",
                "model": chat,
                "base_url": f"{endpoint.rstrip('/')}/deployments/{chat}",
                "label": lbl,
                "provider": "gateway",
                "max_retries": 2,
                "context_window": context_window,
                "max_output_tokens": max_output_tokens,
                **extras,
            }
            for t, lbl in tier_labels.items()
        }
        embed_entry = {
            "api_style": "openai",
            "model": embed,
            "base_url": f"{endpoint.rstrip('/')}/deployments/{embed}",
            "dimension": dim,
            "max_retries": 2,
            "max_input_chars": 8000,
            "max_batch_size": 96,
            **extras,
        }
    else:
        llm_models = {
            f"chat-{t}": {
                "api_style": "litellm",
                "model": f"azure/{chat}",
                "base_url": endpoint,
                "api_key_ref": LLM_KEY_REF,
                "label": lbl,
                "provider": "azure-openai",
                "max_retries": 2,
            }
            for t, lbl in tier_labels.items()
        }
        embed_entry = {
            "api_style": "litellm",
            "model": f"azure/{embed}",
            "base_url": endpoint,
            "api_key_ref": EMBED_KEY_REF,
            "dimension": dim,
            "max_retries": 2,
            "max_input_chars": 8000,
            "max_batch_size": 96,
        }
    # Matryoshka: ask the provider for `dim`-wide vectors instead of the model's native
    # width (needs a bundle whose proxy honors it). Defaults on for a
    # text-embedding-3-* model so the recommended install truncates to the baked width;
    # an explicit embedding.requestDimensions wins. Omitted when false so the values
    # validate against an older bundle's schema.
    req_dims = opt(inp, "embedding.requestDimensions", None)
    if req_dims is None:
        req_dims = embed.lower().startswith("text-embedding-3")
    if req_dims:
        embed_entry["request_dimensions"] = True
    embedding_models = {embed: embed_entry}
    rerank_models = {
        "rerank": {
            "api_style": "litellm",
            "model": rerank_model,
            "base_url": rerank_base_url,
            "api_key_ref": RERANK_KEY_REF,
            "max_retries": 2,
            "max_query_chars": 1000,
            "max_doc_chars": 800,
            "max_docs_per_request": 100,
            # never set api_version on a litellm rerank model — the proxy rejects it.
        }
    }
    provider_keys = {RERANK_KEY_REF: ""}
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
        # The gateway publishes no rerank route yet, so rerank keeps its own
        # endpoint and static key. Say so rather than let it look configured.
        sys.stderr.write(
            "gen-values: note: inference.gateway applies to chat + embedding only; "
            "rerank still uses inference.rerankEndpoint with its own key.\n"
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
        provider_keys[LLM_KEY_REF] = ""
        provider_keys[EMBED_KEY_REF] = ""

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
                    "embedding": embed,
                    "rerank": "rerank",
                },
                # Empty stubs; real values are --set at install (never written here).
                "providerKeys": provider_keys,
            },
        }
    }
    if gw:
        values["nexus"]["inference"]["credentials"] = {
            GATEWAY_CREDENTIAL: credential_entry,
        }
    return values


def build_inputs_env(inp, dim, outdir):
    """Non-secret scalars install.sh / image-manifest.sh / create-secrets.sh consume."""
    idx_id = req(inp, "staticIndex.id")
    provider = storage_provider(inp)
    baked_dim = int(req(inp, "bundle.bakedDimension"))
    baked_id = req(inp, "bundle.bakedIndexId")
    # Path decision: OCI cannot override the baked data-plane dimension or index id.
    oci_ok = (int(dim) == baked_dim) and (idx_id == baked_id)
    env = {
        "KUBE_CONTEXT": req(inp, "kubeContext"),
        "REGISTRY_BASE": req(inp, "registry.base"),
        "REGISTRY_SERVER": req(inp, "registry.server"),
        "REGISTRY_USERNAME": req(inp, "registry.username"),
        "REGISTRY_PASSWORD_ENV": req(inp, "registry.passwordEnv"),
        "PULL_SECRET_NAME": opt(inp, "registry.pullSecretName", "acr-pull"),
        "BUNDLE_TAG": str(req(inp, "bundle.tag")),
        "CHART_VERSION": f"0.0.0-bundle.{req(inp, 'bundle.tag')}",
        "RERANK_KEY_ENV": req(inp, "inference.rerankKeyEnv"),
        "LLM_KEY_REF": LLM_KEY_REF,
        "EMBED_KEY_REF": EMBED_KEY_REF,
        "RERANK_KEY_REF": RERANK_KEY_REF,
        "STATIC_INDEX_ID": idx_id,
        "EMBED_DIMENSION": str(dim),
        "BAKED_DIMENSION": str(baked_dim),
        "BAKED_INDEX_ID": baked_id,
        "INSTALL_PATH": "oci" if oci_ok else "local",
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
    gw = gateway_spec(inp)
    env["GATEWAY_ENABLED"] = "1" if gw else "0"
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
        env["LLM_KEY_ENV"] = req(inp, "inference.llmKeyEnv")
        env["EMBEDDING_KEY_ENV"] = req(inp, "inference.embeddingKeyEnv")

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
        "# GENERATED by gen-values.py — top-level overrides the OCI chart's values.yaml\n"
        "# cannot carry (staticIndex, global.image, ingress, nexus.config index/embed).\n",
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
        "# providerKeys are empty stubs; real keys are --set at install time.\n",
    )
    build_inputs_env(inp, dim, args.outdir)


if __name__ == "__main__":
    main()
