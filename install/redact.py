#!/usr/bin/env python3
"""Redact credential-shaped text from a collected support bundle, in place.

support-bundle.sh runs this over the whole bundle directory before the tarball is
written, then reports the per-pattern counts in REDACTIONS.txt.

Three layers, because no single one is sufficient:
  * structural — values under a Secret's `data:`/`stringData:` and under the chart's
    `providerKeys:` are dropped wholesale. `helm get values` is credential-bearing by
    construction (install.sh injects five credentials with --set), so that block is
    not left to pattern matching.
  * key names — an assignment whose key ends in a credential word loses its value.
    Keys on the REFERENCE_KEYS list hold an identifier or the *name* of a secret,
    so they are kept: losing them would hide the input that broke.
  * patterns — headers, connection strings, SAS signatures, JWTs and JSON pairs,
    for unstructured log output where neither layer above applies.

A file that cannot be scanned is deleted rather than shipped unredacted; undecodable
bytes are replaced so one bad byte does not cost a whole log.

Usage: redact.py DIR        rewrite DIR in place, print a per-pattern hit count
       redact.py --self-test
"""

import argparse
import json
import os
import re
import sys

PLACEHOLDER = "[REDACTED]"
TEXT_SUFFIXES = {".yaml", ".yml", ".json", ".log", ".txt", ".env", ".md", ""}
MAX_BYTES = 64 * 1024 * 1024

SENSITIVE_SUFFIXES = (
    "password",
    "passwd",
    "secret",
    "credential",
    "token",
    "cookie",
    "bearer",
    "key",
)

# Keys that hold an identifier or the name of a secret, never credential material.
# Kept readable because they are what makes a bundle diagnosable, and exempt from
# long-hex-string, which their values would otherwise trip. `secretkey` is the ESO field
# naming a key inside a Secret, not a value — safe unless the stack ever stores a real
# credential under that exact key.
REFERENCE_KEYS = (
    "existingsecret",
    "secretkey",
    "gitcommit",
    "sourceversion",
    "machineid",
    "processid",
    "traceid",
    "requestid",
)
HEX_KEY = re.compile(r"[0-9a-f]{32,}")

REFERENCE_VALUE = re.compile(r"^<set to the key ")
EMPTY_VALUE = re.compile(r"^(?:\"\"|'')$")
BLOCK_SCALAR = re.compile(r"^[|>](?:[1-9][-+]?|[-+][1-9]?)?(?:[ \t]+#.*)?$")

# This annotation echoes a whole prior object, so it can smuggle a secret past every
# other layer; it only duplicates the live spec we already collect, so drop it wholesale.
OPAQUE_DUMP_KEY = re.compile(r"(?i)(?:^|[./])last-applied-configuration$")

QUOTE = r"[\"']?"
PATTERNS = [
    (
        "authorization-header",
        re.compile(
            r"(?i)\b(authorization[\"']?\s*[:=]\s*" + QUOTE + r"(?:bearer\s+|basic\s+)?)([^\s\"',]+)"
        ),
        r"\1" + PLACEHOLDER,
    ),
    (
        "api-key-header",
        re.compile(r"(?i)\b((?:x-)?api[-_]?key[\"']?\s*[:=]\s*" + QUOTE + r")([^\s\"',]+)"),
        r"\1" + PLACEHOLDER,
    ),
    # Recovers an Azure AD client secret the assignment layer misses when it is not the
    # first key on a line; specific enough to match anywhere without eating prose.
    (
        "client-secret",
        re.compile(r"(?i)\b(client[_-]?secret[\"']?\s*[:=]\s*" + QUOTE + r")([^\s\"',;]+)"),
        r"\1" + PLACEHOLDER,
    ),
    (
        "json-credential-pair",
        re.compile(
            r"(?i)(\"[A-Za-z0-9_.-]*(?:password|passwd|secret|credential|token|cookie|key)\"\s*:\s*)"
            r"\"[^\"]+\""
        ),
        r'\1"' + PLACEHOLDER + '"',
    ),
    (
        "azure-account-key",
        re.compile(r"(?i)\b(accountkey=)([^;\s\"']+)"),
        r"\1" + PLACEHOLDER,
    ),
    (
        "connection-string-password",
        re.compile(r"(?i)\b(password=)([^;\s\"']+)"),
        r"\1" + PLACEHOLDER,
    ),
    (
        "sas-signature",
        re.compile(r"(?i)([?&]sig=)([^&\s\"']+)"),
        r"\1" + PLACEHOLDER,
    ),
    (
        "credential-token",
        re.compile(r"\b(?:pcsk_|sk-ant-|sk-proj-|sk-)[A-Za-z0-9_-]{12,}"),
        PLACEHOLDER,
    ),
    (
        "jwt",
        re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+"),
        PLACEHOLDER,
    ),
    (
        "long-hex-string",
        re.compile(r"(?<![:/@0-9a-f])\b[0-9a-f]{32,}\b"),
        PLACEHOLDER,
    ),
]

ASSIGNMENT = re.compile(r"^(\s*-?\s*[\"']?)([A-Za-z0-9_./ -]+?)([\"']?\s*[:=]\s*)(\S.*?)\s*$")
DOCUMENT_BREAK = re.compile(r"^(?:---\s*$|-?\s*apiVersion:)")
KIND = re.compile(r"^\s*-?\s*kind:\s*(\S+)\s*$")
SECRET_DATA_BLOCK = re.compile(r"^(\s*)(?:data|stringData):\s*$")
PROVIDER_KEY_BLOCK = re.compile(r"^(\s*)providerKeys:\s*$")
ENV_NAME = re.compile(r"^\s*-\s*name:\s*[\"']?([A-Za-z0-9_.-]+)[\"']?\s*$")


def _normalize(key):
    return re.sub(r"[^a-z0-9/]", "", key.lower())


def _is_sensitive_key(key):
    return _normalize(key).endswith(SENSITIVE_SUFFIXES)


def _is_reference_key(key):
    normalized = _normalize(key)
    return (
        normalized in ("key", "id")
        or normalized.startswith("checksum/")
        or normalized.endswith(REFERENCE_KEYS)
        or HEX_KEY.fullmatch(normalized) is not None
    )


# Keeps the quoting and the trailing comma so a redacted JSON file still parses.
def _placeholder_for(value):
    trailer = "," if value.endswith(",") else ""
    core = value[: -len(trailer)] if trailer else value
    if len(core) >= 2 and core[0] in "\"'" and core[-1] == core[0]:
        return f"{core[0]}{PLACEHOLDER}{core[0]}{trailer}"
    return f"{PLACEHOLDER}{trailer}"


def _count(counts, name, hits=1):
    counts[name] = counts.get(name, 0) + hits


def _apply_patterns(line, counts, skip=()):
    for name, pattern, replacement in PATTERNS:
        if name in skip:
            continue
        line, hits = pattern.subn(replacement, line)
        if hits:
            _count(counts, name, hits)
    return line


def _redact_assignment(match, counts, label):
    indent, key, separator, value = match.groups()
    if EMPTY_VALUE.match(value):
        return None
    _count(counts, label)
    return f"{indent}{key}{separator}{_placeholder_for(value)}"


# A key containing a space is prose ("invalid token: ..."), not an assignment; wiping
# the rest of the line would destroy the error message, so only the patterns run there.
def redact_line(line, counts):
    match = ASSIGNMENT.match(line)
    if match:
        key, value = match.group(2), match.group(4)
        if OPAQUE_DUMP_KEY.search(key):
            return _redact_assignment(match, counts, "opaque-dump") or line
        if _is_reference_key(key) or REFERENCE_VALUE.match(value):
            return _apply_patterns(line, counts, skip=("long-hex-string",))
        if _is_sensitive_key(key) and not (" " in key and " " in value):
            return _redact_assignment(match, counts, "sensitive-assignment") or line
    return _apply_patterns(line, counts)


def _indent_of(line):
    return len(line) - len(line.lstrip())


def _split_documents(lines):
    documents, current = [], []
    for line in lines:
        if DOCUMENT_BREAK.match(line) and current:
            documents.append(current)
            current = []
        current.append(line)
    documents.append(current)
    return documents


def _document_kind(lines):
    for line in lines:
        match = KIND.match(line)
        if match:
            return match.group(1)
    return None


def redact_text(text, counts):
    return "\n".join(
        line
        for document in _split_documents(text.split("\n"))
        for line in _redact_document(document, _document_kind(document), counts)
    )


# kubectl serialises keys alphabetically, so `data:` precedes `kind:` and the document's
# kind has to be known before the first line is rewritten.
def _redact_document(lines, kind, counts):
    redacted = []
    block = None
    scalar_body = None
    sensitive_env_name = False

    for line in lines:
        indent = _indent_of(line)

        if scalar_body is not None:
            if line.strip() == "" or indent > scalar_body:
                _count(counts, "block-scalar-line")
                continue
            scalar_body = None

        if block is not None and line.strip() != "" and indent <= block[0]:
            block = None

        assignment = ASSIGNMENT.match(line)
        if block is not None and assignment:
            emitted = _redact_assignment(assignment, counts, block[1]) or line
        elif sensitive_env_name and assignment and assignment.group(2) == "value":
            emitted = _redact_assignment(assignment, counts, "env-literal") or line
        else:
            emitted = redact_line(line, counts)
        redacted.append(emitted)

        if emitted != line and assignment and BLOCK_SCALAR.match(assignment.group(4)):
            scalar_body = indent

        if kind == "Secret" and SECRET_DATA_BLOCK.match(line):
            block = (indent, "secret-data")
        elif PROVIDER_KEY_BLOCK.match(line):
            block = (indent, "provider-key")

        env_name = ENV_NAME.match(line)
        sensitive_env_name = bool(env_name) and _is_sensitive_key(env_name.group(1))

    return redacted


def redact_file(path, counts):
    try:
        if os.path.getsize(path) > MAX_BYTES:
            return False
        with open(path, encoding="utf-8", errors="replace") as handle:
            original = handle.read()
    except OSError:
        return False

    text = redact_text(original, counts)
    if text != original:
        temporary = f"{path}.redacting"
        with open(temporary, "w", encoding="utf-8") as handle:
            handle.write(text)
        os.replace(temporary, path)
    return True


def redact_tree(root):
    counts, removed = {}, []
    for dirpath, _, filenames in os.walk(root):
        for filename in sorted(filenames):
            path = os.path.join(dirpath, filename)
            scannable = os.path.splitext(filename)[1].lower() in TEXT_SUFFIXES
            if scannable and redact_file(path, counts):
                continue
            removed.append(os.path.relpath(path, root))
            os.remove(path)
    return counts, removed


def report(counts, removed):
    lines = [
        "# Redaction pass over this bundle. Values matching a credential shape",
        f"# were replaced with {PLACEHOLDER}.",
        "",
    ]
    if counts:
        lines += [f"{name:<28} {counts[name]}" for name in sorted(counts)]
    else:
        lines.append("(no matches)")
    if removed:
        lines += [
            "",
            "# removed, not shipped (binary or oversized, so unscannable):",
        ] + [f"  {path}" for path in removed]
    return "\n".join(lines) + "\n"


SELF_TEST_REDACTED = [
    "Authorization: Bearer abc.def.ghi",
    "authorization=Basic Zm9vOmJhcg==",
    "x-api-key: sk-live-0123456789",
    "inference 401: request had header x-api-key: 3f9aQ2vBzL2x",
    "DefaultEndpointsProtocol=https;AccountName=acme;AccountKey=Zm9vYmFy+baz==;",
    "https://acme.blob.core.windows.net/c?se=2026-01-01&sig=abcDEF%2F123",
    "session expired for eyJhbGciOiJIUzI1NiJ9.cGF5bG9hZA.c2ln",
    "Server=db;Password=hunter2;",
    "ERROR auth failed for key pcsk_6X2abc_RealKeyMaterial123456789",
    "chat call failed with sk-ant-api03-RealAnthropicKeyMaterial",
    "azure returned 401 for 5f4dcc3b5aa765d61d8327deb882cf99",
    '{"password":"hunter2"}',
    "  jwtSecret: 5f4dcc3b5aa765d61d8327deb882cf99",
    "  access_key: wJalrXUtnFEMI",
    "  password: 123456",
    "  jwtSecret: true",
    "Set-Cookie: nexus_session=opaquevalue; Path=/",
    "  connectionStringName: AccountKey=realkey123;",
    'msg="auth failed" client_secret=AbC123RealClientSecret',
    "  client-secret: RealAzureAppSecretValue",
]

SELF_TEST_KEPT = [
    "  storageKeyEnv: NEXUS_STORAGE_KEY",
    "  apiKeyRef: azure_api_key",
    "  pullSecretName: acr-pull",
    "  existingSecret: nexus-azure-storage",
    "  secretKey: azure-storage-access-key",
    "  clusterSecretStore: azure-keyvault",
    "  clientSecretEnv: NEXUS_CLIENT_SECRET",
    "image: reg.example.com/nexus_api@sha256:"
    "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
    "  gitCommit: 1ad6e68c4d5c2b8f3a9e7d6c5b4a39281f0e7d6c",
    "        checksum/byoc-config: 3a7bd3e2360a3d29eea436fcfb7e44c735d117c42d1c1835420b6b9942dd4f1b",
    "  Machine ID:                 8ee1c6a2f0b34d5e9a7c1b2d3e4f5a6b",
    "  trace_id: 4bf92f3577b34da6a3ce929d0e0e4736",
    "      PINECONE_AUTH__JWT_SECRET:  <set to the key 'jwt-secret' in secret 'nexus-config'>",
    "    TokenExpirationSeconds:  3607",
    "  Token bucket refill rate: 100/s",
    "              key: jwt-secret",
    '  claudeApiKey: ""',
    "invalid token: token has invalid claims: token is expired by 3h21m18s",
    'failed to get secret: secrets "nexus-inference-keys" is forbidden: User'
    ' "system:serviceaccount:nexus:nexus-api" cannot get resource "secrets"',
    '                    "processid" : "9c1e3b5d7f9a1c3e5b7d9f1a3c5e7b9d",',
    '                    "source_version" : "e1b5b09a5b4b09e6cf5cd8b2e14e2d2c7d0e2b16",',
    '                            "id" : "5b7d9f1a3c5e7b9d1f3a5c7e9b1d3f5a",',
    '            "9c1e3b5d7f9a1c3e5b7d9f1a3c5e7b9d" : {',
]

BLOCK_SCALAR_YAML = """apiVersion: v1
kind: Secret
metadata:
  name: nexus-config
stringData:
  password: |
    hunter2
    second-line-secret
  indented: |2-
      hunter3
  commented: | # rotated
    hunter4
  plain: abc
---
apiVersion: v1
kind: ConfigMap
data:
  gateway.conf: |
    server { listen 80; }
"""

SECRET_LIST_YAML = """apiVersion: v1
items:
- apiVersion: v1
  data:
    azure-storage-access-key: UkVBTEtFWQ==
  kind: Secret
  metadata:
    name: nexus-azure-storage
  type: Opaque
- apiVersion: v1
  data:
    gateway.conf: c2VydmVyIHt9
  kind: ConfigMap
  metadata:
    name: nexus-gateway-conf
kind: List
"""

PROVIDER_KEYS_YAML = """nexus:
  inference:
    providerKeys:
      llm-key: sk-proj-realone
      embedding-key: sk-proj-realtwo
      rerank-key: sk-realthree
    endpoint: https://acme.cognitiveservices.azure.com
"""

ENV_YAML = """        - name: PINECONE_PINECONE__API_KEY
          value: pcsk_live_value
        - name: PINECONE_SERVER__PORT
          value: "9000"
"""

JSON_DOCUMENT = """{
    "cluster": {
        "api_key": "sk-proj-realvalue",
        "generation": 12
    }
}
"""

# Proves the annotation is dropped whole (block and inline) while the real spec below
# it still redacts as usual.
LAST_APPLIED_BLOCK_YAML = """apiVersion: apps/v1
kind: Deployment
metadata:
  name: nexus-api
  annotations:
    kubectl.kubernetes.io/last-applied-configuration: |
      {"env":[{"name":"JWT_SECRET","value":"annotation-plain-jwt"}]}
spec:
  template:
    spec:
      containers:
      - name: api
        env:
        - name: JWT_SECRET
          value: spec-plain-jwt
"""

LAST_APPLIED_INLINE_YAML = (
    "apiVersion: v1\n"
    "kind: ConfigMap\n"
    "metadata:\n"
    "  name: c\n"
    "  annotations:\n"
    "    kubectl.kubernetes.io/last-applied-configuration: "
    '\'{"data":{"note":"annotation-plain-value"}}\'\n'
    "data:\n"
    "  note: hello\n"
)


def _check(failures, condition, message):
    if not condition:
        failures.append(message)


def self_test():
    failures = []
    for line in SELF_TEST_REDACTED:
        _check(failures, PLACEHOLDER in redact_line(line, {}), f"not redacted: {line}")
    for line in SELF_TEST_KEPT:
        result = redact_line(line, {})
        _check(failures, result == line, f"wrongly redacted: {line} -> {result}")

    counts = {}
    secret_list = redact_text(SECRET_LIST_YAML, counts)
    _check(failures, "UkVBTEtFWQ==" not in secret_list, "Secret value survived in List output")
    _check(failures, counts.get("secret-data") == 1, "Secret data was not counted")
    _check(failures, "c2VydmVyIHt9" in secret_list, "ConfigMap data was redacted")
    _check(failures, "azure-storage-access-key" in secret_list, "Secret key name was lost")

    counts = {}
    provider = redact_text(PROVIDER_KEYS_YAML, counts)
    _check(failures, "sk-proj-realone" not in provider, "provider llm key survived")
    _check(failures, "sk-proj-realtwo" not in provider, "provider embedding key survived")
    _check(failures, "sk-realthree" not in provider, "provider rerank key survived")
    _check(failures, counts.get("provider-key") == 3, "provider keys were not counted")
    _check(failures, "cognitiveservices" in provider, "value outside the block was redacted")

    counts = {}
    env = redact_text(ENV_YAML, counts)
    _check(failures, "pcsk_live_value" not in env, "literal env secret was not redacted")
    _check(failures, counts.get("env-literal") == 1, "env literal was not counted")
    _check(failures, '"9000"' in env, "non-sensitive env value was redacted")

    counts = {}
    block_applied = redact_text(LAST_APPLIED_BLOCK_YAML, counts)
    _check(failures, "annotation-plain-jwt" not in block_applied, "last-applied block survived")
    _check(failures, "spec-plain-jwt" not in block_applied, "spec env value survived")
    _check(failures, counts.get("opaque-dump") == 1, "last-applied annotation not counted")
    _check(failures, counts.get("env-literal") == 1, "spec env literal was not redacted")

    inline_applied = redact_text(LAST_APPLIED_INLINE_YAML, {})
    _check(failures, "annotation-plain-value" not in inline_applied, "inline last-applied survived")
    _check(failures, "note: hello" in inline_applied, "data outside the annotation was redacted")

    counts = {}
    scalars = redact_text(BLOCK_SCALAR_YAML, counts)
    for body in ("hunter2", "second-line-secret", "hunter3", "hunter4"):
        _check(failures, body not in scalars, f"block-scalar body survived: {body}")
    _check(failures, counts.get("block-scalar-line") == 4, "block-scalar body was not counted")
    _check(failures, "plain: [REDACTED]" in scalars, "value after the block scalar was skipped")
    _check(failures, "server { listen 80; }" in scalars, "ConfigMap block scalar was dropped")

    redacted_json = redact_text(JSON_DOCUMENT, {})
    _check(failures, "sk-proj-realvalue" not in redacted_json, "JSON secret survived")
    try:
        _check(
            failures,
            json.loads(redacted_json)["cluster"]["api_key"] == PLACEHOLDER,
            "redacted JSON lost its value",
        )
    except json.JSONDecodeError as error:
        failures.append(f"redacted JSON no longer parses: {error}")

    inline = redact_line('{"a":1,"password":"hunter2"}', {})
    try:
        _check(failures, json.loads(inline)["password"] == PLACEHOLDER, "inline JSON lost its value")
    except json.JSONDecodeError as error:
        failures.append(f"redacted inline JSON no longer parses: {error}")

    for failure in failures:
        print(f"FAIL: {failure}", file=sys.stderr)
    print(f"self-test: {len(failures)} failure(s)", file=sys.stderr)
    return 1 if failures else 0


def main():
    parser = argparse.ArgumentParser(description="Redact secrets from a support bundle, in place.")
    parser.add_argument("directory", nargs="?", help="bundle directory to rewrite")
    parser.add_argument("--self-test", action="store_true", help="verify the patterns and exit")
    args = parser.parse_args()

    if args.self_test:
        return self_test()
    if not args.directory:
        parser.error("a bundle directory is required")
    if not os.path.isdir(args.directory):
        parser.error(f"not a directory: {args.directory}")

    counts, removed = redact_tree(args.directory)
    sys.stdout.write(report(counts, removed))
    return 0


if __name__ == "__main__":
    sys.exit(main())
