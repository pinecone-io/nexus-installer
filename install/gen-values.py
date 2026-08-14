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
    values = {
        "staticIndex": {
            "id": idx_id,
            "name": opt(inp, "staticIndex.name", "nexus-hybrid"),
            "dimension": dim,
        },
        "global": {
            "image": {
                "registry": req(inp, "registry.base"),
                "pullSecrets": [{"name": opt(inp, "registry.pullSecretName", "acr-pull")}],
            }
        },
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


# The db-slim data-dir overlay is a required part of the abs profile: the base
# renders ssd-volume as an emptyDir, so --data-dir must sit at the mount root or the
# service ENOENTs at startup. Copied verbatim from the chart's values.abs.yaml so the
# generated overlay stays faithful to the reference contract.
DBSLIM_ABS_OVERLAY = {
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
        "db-slim": DBSLIM_ABS_OVERLAY,
        # pc-blob picks its driver from cloud.provider, not the blob backend.
        "nexus": {"config": {"cloud": {"provider": "azure"}, "storage": {"localRoot": ""}}},
    }


def build_self_hosted_values(inp, dim):
    endpoint = req(inp, "inference.endpoint")
    rerank_endpoint = req(inp, "inference.rerankEndpoint")
    chat = req(inp, "inference.chatDeployment")
    embed = req(inp, "inference.embeddingDeployment")
    rerank = req(inp, "inference.rerankDeployment")

    # the proxy requires each tier to resolve to a distinct model ref
    tier_labels = {"lite": "Chat (lite)", "standard": "Chat (standard)", "pro": "Chat (pro)"}
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
            "model": f"cohere/{rerank}",
            "base_url": rerank_endpoint,
            "api_key_ref": RERANK_KEY_REF,
            "max_retries": 2,
            "max_query_chars": 1000,
            "max_doc_chars": 800,
            "max_docs_per_request": 100,
            # never set api_version on a litellm rerank model — the proxy rejects it.
        }
    }
    return {
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
                "providerKeys": {LLM_KEY_REF: "", EMBED_KEY_REF: "", RERANK_KEY_REF: ""},
            },
        }
    }


def build_inputs_env(inp, dim, outdir):
    """Non-secret scalars install.sh / image-manifest.sh / create-secrets.sh consume."""
    idx_id = req(inp, "staticIndex.id")
    auth = opt(inp, "storage.auth", "shared_key")
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
        "STORAGE_ACCOUNT": req(inp, "storage.account"),
        "CONTAINER_PREFIX": req(inp, "storage.containerPrefix"),
        "CONTAINER_NAMES": " ".join(container_names(req(inp, "storage.containerPrefix"))),
        "STORAGE_AUTH": auth,
        "STORAGE_EXISTING_SECRET": opt(inp, "storage.existingSecret", ""),
        "STORAGE_KEY_ENV": opt(inp, "storage.storageKeyEnv", ""),
        "WI_CLIENT_ID": opt(inp, "storage.clientId", ""),
        "LLM_KEY_ENV": req(inp, "inference.llmKeyEnv"),
        "EMBEDDING_KEY_ENV": req(inp, "inference.embeddingKeyEnv"),
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
