# Nexus self-hosted install toolkit

Install Pinecone Nexus (self-hosted) into a Kubernetes cluster you operate, from a
single inputs file. This toolkit turns the manual install — gather ~11 inputs,
hand-populate two values files plus several `--set` flags, create secrets, mirror
images — into: **fill one file → generate → validate → install.**

It automates the whole install. A wrong value in one of the consistency
invariants (embedding dimension, container names, registry override, workload
identity) otherwise produces a green `helm install` that boot-panics or fails on
first ingest; here those mismatches **fail at preflight, before install**.

You can read every script before running anything. Nothing here reaches a cluster
until you run the install step, and `install.sh --dry-run` renders the whole plan
touching nothing.

---

## What's in here

| File | What it does |
|------|--------------|
| `customer.example.yaml` | The inputs contract. Copy to `customer.yaml` and fill it in. Every field maps one-to-one to what it configures. **Secrets are env-var names, never literals.** |
| `gen-values.py` | Reads the inputs and emits the Helm overlays (`values.install.yaml`, `values.abs.yaml`, `values.self-hosted.yaml`) + `inputs.env`. Deterministic, secret-free. |
| `preflight.py` | Validates the consistency invariants. Static by default (values only, no cloud); `--live` adds cluster/Azure checks. **This is the core value.** |
| `create-secrets.sh` | Idempotently creates the namespace, the registry pull Secret, and (shared-key only) the storage-key Secret, from env-var references. Never echoes a value. |
| `install.sh` | Orchestrates preflight → secrets → `helm install`. `--dry-run` renders the full plan without touching anything. |
| `image-manifest.sh` | Prints the exact images + OCI chart the install pulls (writes `generated/manifest.txt`), which `preflight --live` then verifies. Copies nothing — staging them in your registry is your pipeline's job. |
| `remint-dbslim.sh` | Local-chart helper for a non-default embedding dimension / fresh index id (see "OCI vs local-chart path"). |
| `tf-to-inputs.sh` | Emits the storage/identity half of `customer.yaml` from the `aks-slim` Terraform outputs. |

Generated files (`generated/`), your filled `customer.yaml`, and `.secrets.env` are
gitignored.

## Prerequisites you own

Before running anything:

- **A cluster.** Kubernetes 1.35, ≥ 2 amd64 nodes, a default StorageClass, and node
  egress to your registry, storage account, and model endpoints. Greenfield? The
  optional `terraform/aks-slim` module in this repo stands up a conforming AKS cluster.
- **A registry.** The image bundle + the OCI chart available under one base path in
  your ACR/Artifactory, plus a pull credential — staged by your own pipeline (an
  active copy) or served by a pull-through remote. `image-manifest.sh --list` prints
  exactly what must be present.
- **Azure Blob storage.** One StorageV2 account and its **seven containers** (the stack
  does not create them). The optional `terraform/aks-slim` module provisions them for
  you; otherwise create them before install. Names derive from your stem: `<stem>-db`
  plus `<stem>-nexus-{source,knowledge,archive,traces,snapshots,library}`.
- **Model deployments** (chat, embedding, rerank) on OpenAI-compatible endpoints. The
  deployment names must match names the routing library recognizes — chat `gpt-5`,
  embedding `text-embedding-3-small`, and the rerank deployment named literally
  `rerank-v3.5` (even when it serves a newer Cohere rerank model). **The embedding
  model's dimension fixes the index dimension and is immutable after install.**
- **Tooling:** `kubectl`, `helm`, `python3` + PyYAML, `openssl`; `az` for the live
  preflight checks.

## Quick start

```bash
cd install
cp customer.example.yaml customer.yaml
$EDITOR customer.yaml                      # fill in ~11 inputs; secrets are env-var NAMES

# Export the secrets the inputs reference (names are your choice, set in customer.yaml):
export NEXUS_REGISTRY_PASSWORD=...         # registry.passwordEnv
export NEXUS_STORAGE_KEY=...               # storage.storageKeyEnv (shared_key only)
export NEXUS_LLM_KEY=...                   # inference.llmKeyEnv / embeddingKeyEnv
export NEXUS_RERANK_KEY=...                # inference.rerankKeyEnv

# 1. Generate overlays + validate (no cluster access needed):
python3 gen-values.py
python3 preflight.py                       # static invariants; fix any FAIL before continuing

# 2. List the images + chart the install pulls, and make sure they are present in your
#    registry — staged by your own pipeline (an active copy) or served by a pull-through
#    remote. This copies nothing.
./image-manifest.sh --list                 # add --chart-path <chart> for exact refs

# 3. Optional live checks (containers, image presence, identity in the cloud):
python3 preflight.py --live

# 4. Dry-run the whole install (touches nothing), then install for real:
./install.sh --dry-run
./install.sh
```

`install.sh` regenerates the overlays and re-runs the static preflight itself, so
steps 1–3 are optional before step 4 — they're there so you can inspect the artifacts
and confirm your registry first.

## Verify the install

When `install.sh` returns, confirm the stack is healthy and then exercise it end to end.

1. **Pods.** Every pod is `Running` / `Ready` (14 total):

   ```bash
   kubectl --context <kubeContext> -n <namespace> get pods
   ```

2. **API reachable.** Through your ingress — or, before ingress is wired, a
   `kubectl port-forward` to the `nexus-gateway` Service — the version endpoint answers:

   ```bash
   curl -fsS https://<your-host>/api/v0/version
   ```

3. **Functional smoke test.** Open the console at your host and sign in with the session
   credential (`install.sh` writes it to `.secrets.env`). Create a context, add a source,
   run curate, then run a query. A successful answer with citations exercises the whole
   path — embedding on ingest, the vector index on query, and chat + rerank through the
   inference proxy to your endpoints — and is the check that confirms your model endpoints
   are correctly wired.

## The inputs, one-to-one

Every field in `customer.example.yaml` carries a comment naming exactly what it
configures. The important ones:

- `embedding.dimension` — the single source for `staticIndex.dimension`,
  `nexus.config.indexMetadata.dimension`, and `nexus.config.embeddingModel.dimension`.
  It must equal the width your embedding model actually emits. `text-embedding-3-small`
  emits 1536 natively but is a Matryoshka model: with `embedding.requestDimensions: true`
  the proxy asks it for `dimension`-wide (1024) vectors, so the recommended model stays
  at the chart's baked 1024 and installs over OCI with no re-mint (needs a bundle whose
  proxy honors the dimensions request).
- `storage.containerPrefix` — the stem the seven container names derive from.
- `storage.auth` — `shared_key` (an account-key Secret) or `workload_identity` (keyless;
  needs `clientId`, the user-assigned managed identity).
- `registry.base` — the flat base every image and the chart live under; becomes the
  chart's single `global.image.registry` knob.
- `bundle.tag` — identifies the release; it is the OCI chart version suffix
  (`0.0.0-bundle.<tag>`).

Secrets never appear here. A `*Env` field names the environment variable that holds the
secret; the tools read it from your shell and never log it.

## What preflight checks

Static (values only, always safe):

- **Dimension agreement** — the embedding dimension equals every place the dimension
  appears; and if it (or the index id) differs from the bundle's baked value, it fails
  for the OCI path with the reason and the fix (see below).
- **Container prefix** — the seven containers derive from the stem.
- **Inference catalog** — the self-hosted profile is selected, every `api_key_ref` has a
  `providerKeys` entry, and all tier slots resolve to a defined catalog entry.
- **Registry** — the image override is set and the pull-secret server matches the base.
- **Storage auth** — `workload_identity` has a `clientId`; `shared_key` has an
  `existingSecret`.

Live (`--live`, opt-in, needs `az`/`kubectl` + the `azure.*` inputs):

- the kube context is reachable, the seven containers exist, every bundle image is
  present in your mirror at the expected tag, and (workload identity) a federated
  credential covers the release service account.

## OCI vs local-chart path

The install defaults to the **published OCI chart** (`oci://<your-registry>/nexus-installer
--version 0.0.0-bundle.<tag>`). One known limit: the OCI chart **bakes the data-plane
embedding dimension and the static index id** into its generated DB values at build
time, and the OCI path cannot override them. So:

- **Dimension and index id equal the bundle's baked values** → OCI path, fully automated.
  The recommended `text-embedding-3-small` at 1024 (via `embedding.requestDimensions`,
  above) lands here — a Matryoshka model truncating to the baked dimension keeps it on
  the OCI path, so this is now the default rather than a fallback.
- **A non-Matryoshka model whose native width isn't the baked dimension** (reduction can't
  reach it), or **a freshly minted index id** → the **local-chart path**. `install.sh`
  selects it automatically;
  preflight tells you, and the flow is:

  ```bash
  # against the chart checkout Pinecone provides:
  ./remint-dbslim.sh --chart-path /path/to/chart          # sets dim + id, regenerates DB values
  ./install.sh --path local --chart-path /path/to/chart
  ```

Making the data-plane dimension a runtime value is a prerequisite for a fully
OCI-based install at the recommended embedding model; until then the toolkit falls back
automatically.

## Terraform hand-off (greenfield)

If you provisioned the cluster with `terraform/aks-slim`, its outputs fill the
storage/identity inputs for you:

```bash
./tf-to-inputs.sh                 # prints a YAML fragment; merge it into customer.yaml
```

Mapping: `blob_storage_account → storage.account`, `blob_container →
storage.containerPrefix`, `workload_identity_client_id → storage.clientId` (and it
defaults `storage.auth: workload_identity`, since the module wires keyless federation),
`cluster_name → kubeContext`.

## Where Pinecone takes over (hand-off points)

This toolkit gets you to a healthy, verified release. The points where a Pinecone
engineer typically finishes hands-on:

1. **Ingress.** The gateway is `ClusterIP` by default; set `ingress.*` and wire your
   controller/DNS/TLS, or Pinecone helps expose it.
2. **First functional verification** (ingest → curate → query → chat) against your real
   model endpoints — see **Verify the install** above.
3. **Model endpoint tuning** (e.g. raising the rerank deployment's throughput quota).

## Notes

- **Install-only chart.** Never `helm upgrade` this release. To iterate: uninstall,
  delete the PVCs, and re-run. `install.sh` persists the generated JWT + session
  credentials to `.secrets.env` (0600) and reuses them, so re-installs keep stable
  credentials — the session credential is your API login; keep the file safe.
- **Idempotent.** `create-secrets.sh` and the mirror are safe to re-run.
- Expect ~20 helm client warnings like `hides previous definition of …` during install
  — they are by design and harmless.
