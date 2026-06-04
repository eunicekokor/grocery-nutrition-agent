#!/usr/bin/env bash
#
# test_remote_endpoint.sh
#
# Smoke-tests the `generative.LlmGenerateService/TestRemoteEndpoint` gRPC RPC on
# the generative sidecar. The RPC POSTs to a caller-supplied endpoint without a
# persisted remote_endpoint_integrations row (a "pre-creation probe").
#
# The interesting/fragile part is `encrypted_headers`: the proto field is an
# encrypted string, NOT plaintext. The generative server decrypts it with
# EXTERNAL_LLM_API_KEY_ENCRYPTION_KEY using AES-256-CBC. This script reproduces
# the exact ciphertext format the server's decrypt() expects:
#
#   - algorithm: aes-256-cbc
#   - key:       the EXTERNAL_LLM_API_KEY_ENCRYPTION_KEY string, passed straight
#                to createCipheriv (so it MUST be exactly 32 bytes — same as the
#                server, which passes config.EXTERNAL_LLM_API_KEY_ENCRYPTION_KEY
#                directly without hashing)
#   - iv:        16 random bytes
#   - payload:   JSON.stringify of a flat string->string header map
#   - output:    ivHex + ciphertextHex  (iv prefixed, all hex, no separator)
#   - AAD:       none (the "context" arg in decrypt() is only used for logging)
#
# See:
#   arizeweb/src/server/utils/encryption.ts            (encrypt/decrypt format)
#   arizeweb/src/server/remoteEndpointIntegrations/utils.ts (encryptHeadersOrThrow)
#   arizeweb/src/server/generative/callRemoteEndpoint.ts    (decryptHeaders + POST)
#
# NOTE: Content-Type: application/json is added automatically by the server
# (callRemoteEndpoint.ts), so encrypted_headers only needs the Authorization
# header.
#
# ---------------------------------------------------------------------------
# Prerequisites
# ---------------------------------------------------------------------------
#   1. grpcurl installed:            brew install grpcurl
#   2. node installed (for the AES encryption step)
#   3. The generative sidecar reachable on localhost:6018, either via:
#        - `pnpm run dev:generative`  (from arizeweb/), OR
#        - a port-forward to the generative pod:
#            kubectl -n mynamespace port-forward deploy/generative 6018:6018
#      The sidecar speaks plaintext gRPC with NO server reflection, so we point
#      grpcurl at the proto files explicitly:
#          -import-path proto -proto generative/llm_generate.proto
#
# ---------------------------------------------------------------------------
# Usage
# ---------------------------------------------------------------------------
#   export BEARER_TOKEN="your-railway-bearer-token"
#   # MUST match the key the running generative server was started with:
#   export EXTERNAL_LLM_API_KEY_ENCRYPTION_KEY="32-byte-key-xxxxxxxxxxxxxxxxxxxx"
#   # Get the key from the secret:
#   #   kubectl --context=dev -n arize-dev get secret \
#   #     external-llm-api-key-encryption-key \
#   #     -o jsonpath='{.data.external_llm_api_key_encryption_key}' | base64 -d; echo
#   # optional overrides:
#   export SPACE_ID=1              # default 1
#   export GRPC_TARGET=localhost:6018
#
#   # Reference-free LLM judges (need ANTHROPIC_API_KEY on the server):
#   ./scripts/test_remote_endpoint.sh groundedness_basic
#   ./scripts/test_remote_endpoint.sh --mode reference_free helpfulness_weak
#   ./scripts/test_remote_endpoint.sh --list                          # reference_free cases
#
#   # Golden / reference-based judges (need expected ground truth in the fixture):
#   ./scripts/test_remote_endpoint.sh --mode golden 001_category
#   ./scripts/test_remote_endpoint.sh --mode golden 021_allergen
#   ./scripts/test_remote_endpoint.sh --mode golden --list
#
#   # Mode-agnostic utility cases (root fixtures/ directory):
#   ./scripts/test_remote_endpoint.sh --mode root unknown_eval
#   ./scripts/test_remote_endpoint.sh --mode root minimal
#
# ---------------------------------------------------------------------------
# Available cases (see scripts/fixtures/README.md for full descriptions)
# ---------------------------------------------------------------------------
# mode=reference_free (scripts/fixtures/reference_free/):
#   groundedness_basic         — evidence_groundedness, all claims supported
#   groundedness_hallucination — evidence_groundedness, claims contradict facts
#   helpfulness_strong         — helpfulness, rich specific rationale
#   helpfulness_weak           — helpfulness, vague boilerplate rationale
#
# mode=golden (scripts/fixtures/golden/):
#   001_category   — category_correctness + expected_category: MORE_OF
#   003_dietary    — dietary_safety + expected halal conflict
#   021_allergen   — allergen_recall + expected allergen: avocado
#   022_allergen   — allergen_recall + expected allergen: tree nuts
#
# mode=root (scripts/fixtures/):
#   unknown_eval   — invalid evaluator, expect HTTP 400
#   minimal        — smallest valid payload (required fields only)
# ---------------------------------------------------------------------------

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
FIXTURES_DIR="${SCRIPT_DIR}/fixtures"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

# ---------------------------------------------------------------------------
# Argument parsing: [--mode <reference_free|golden|root>] [--list] [<case_id>]
# ---------------------------------------------------------------------------
MODE="reference_free"   # default
DO_LIST=0
CASE_ID=""

while [[ $# -gt 0 ]]; do
    case "$1" in
        --mode)
            shift
            MODE="${1:?--mode requires a value: reference_free, golden, or root}"
            if [[ "${MODE}" != "reference_free" && "${MODE}" != "golden" && "${MODE}" != "root" ]]; then
                echo "ERROR: --mode must be one of: reference_free, golden, root" >&2
                exit 1
            fi
            ;;
        --list)
            DO_LIST=1
            ;;
        *)
            CASE_ID="$1"
            ;;
    esac
    shift
done

# Resolve the fixture directory for the selected mode.
if [[ "${MODE}" == "root" ]]; then
    MODE_DIR="${FIXTURES_DIR}"
else
    MODE_DIR="${FIXTURES_DIR}/${MODE}"
fi

# ---------------------------------------------------------------------------
# --list: print available case ids for the selected mode and exit
# ---------------------------------------------------------------------------
if [[ "${DO_LIST}" == "1" ]]; then
    echo "Available cases for --mode ${MODE} (${MODE_DIR}/):"
    found=0
    for f in "${MODE_DIR}"/*.json; do
        [[ -f "$f" ]] && printf "  %s\n" "$(basename "$f" .json)" && found=1
    done
    [[ "$found" == "0" ]] && echo "  (none)"
    echo
    echo "Other modes: reference_free | golden | root"
    echo "Usage:  $0 [--mode <mode>] <case_id>"
    echo "        $0 [--mode <mode>] --list"
    exit 0
fi

# ---------------------------------------------------------------------------
# Require CASE_ID
# ---------------------------------------------------------------------------
if [[ -z "${CASE_ID}" ]]; then
    echo "Usage: $0 [--mode reference_free|golden|root] <case_id>" >&2
    echo "       $0 [--mode reference_free|golden|root] --list" >&2
    exit 1
fi

FIXTURE_FILE="${MODE_DIR}/${CASE_ID}.json"
if [[ ! -f "${FIXTURE_FILE}" ]]; then
    echo "ERROR: fixture not found: ${FIXTURE_FILE}" >&2
    echo "       Run '$0 --mode ${MODE} --list' to see available cases." >&2
    exit 1
fi

# --- config / env ----------------------------------------------------------
ENDPOINT="https://grocery-nutrition-agent-production.up.railway.app/evals/v1/evaluate"
GRPC_TARGET="${GRPC_TARGET:-localhost:6018}"
SPACE_ID="${SPACE_ID:-1}"

if [[ -z "${BEARER_TOKEN:-}" ]]; then
    echo "ERROR: BEARER_TOKEN is not set. export BEARER_TOKEN=..." >&2
    exit 1
fi
if [[ -z "${EXTERNAL_LLM_API_KEY_ENCRYPTION_KEY:-}" ]]; then
    echo "ERROR: EXTERNAL_LLM_API_KEY_ENCRYPTION_KEY is not set." >&2
    echo "       It MUST match the key the running generative server uses." >&2
    exit 1
fi

command -v grpcurl >/dev/null 2>&1 || {
    echo "ERROR: grpcurl not found. Install with: brew install grpcurl" >&2
    exit 1
}
command -v node >/dev/null 2>&1 || {
    echo "ERROR: node not found (needed for AES encryption)." >&2
    exit 1
}

# ---------------------------------------------------------------------------
# Load and validate the fixture
# ---------------------------------------------------------------------------
REQUEST_BODY="$(cat "${FIXTURE_FILE}")"

# Validate JSON before sending — fail fast rather than sending malformed payload.
BODY="${REQUEST_BODY}" node -e "JSON.parse(process.env.BODY)" 2>/dev/null || {
    echo "ERROR: ${FIXTURE_FILE} is not valid JSON" >&2
    exit 1
}

# ---------------------------------------------------------------------------
# Encrypt the Authorization header
# ---------------------------------------------------------------------------
# Reproduces arizeweb/src/server/utils/encryption.ts `encrypt()` exactly:
#   iv(16 random bytes) -> cipher(aes-256-cbc, KEY, iv) -> ivHex + ctHex
encrypt_headers() {
    BEARER_TOKEN="${BEARER_TOKEN}" \
    EXTERNAL_LLM_API_KEY_ENCRYPTION_KEY="${EXTERNAL_LLM_API_KEY_ENCRYPTION_KEY}" \
    node -e '
        const { randomBytes, createCipheriv } = require("crypto");
        const ALGORITHM = "aes-256-cbc";
        const BLOCK_SIZE = 16;

        const key = process.env.EXTERNAL_LLM_API_KEY_ENCRYPTION_KEY;
        // The server passes the raw string straight into createCipheriv, which
        // requires a 32-byte key for aes-256. Fail loudly if it is the wrong size
        // so we do not silently produce ciphertext the server cannot decrypt.
        if (Buffer.byteLength(key, "utf8") !== 32) {
            console.error(
                `ERROR: EXTERNAL_LLM_API_KEY_ENCRYPTION_KEY must be exactly 32 bytes, got ${Buffer.byteLength(key, "utf8")}.`
            );
            process.exit(1);
        }

        const headers = { Authorization: `Bearer ${process.env.BEARER_TOKEN}` };
        const text = JSON.stringify(headers);

        const iv = randomBytes(BLOCK_SIZE);
        const cipher = createCipheriv(ALGORITHM, key, iv);
        let ct = cipher.update(text, "utf8", "hex");
        ct += cipher.final("hex");
        process.stdout.write(iv.toString("hex") + ct);
    '
}

# ---------------------------------------------------------------------------
# Build the gRPC request payload
# ---------------------------------------------------------------------------
# jq keeps the JSON well-formed (proper escaping of the request_body string and
# the hex ciphertext). Field names match TestRemoteEndpointCallRequest in
# proto/generative/llm_generate.proto.
build_grpc_payload() {
    local encrypted_headers="$1"
    GRPC_PAYLOAD_ENDPOINT="${ENDPOINT}" \
    GRPC_PAYLOAD_ENC="${encrypted_headers}" \
    GRPC_PAYLOAD_BODY="${REQUEST_BODY}" \
    GRPC_PAYLOAD_SPACE="${SPACE_ID}" \
    node -e '
        const payload = {
            space_id: Number(process.env.GRPC_PAYLOAD_SPACE),
            endpoint: process.env.GRPC_PAYLOAD_ENDPOINT,
            encrypted_headers: process.env.GRPC_PAYLOAD_ENC,
            request_body: process.env.GRPC_PAYLOAD_BODY,
        };
        process.stdout.write(JSON.stringify(payload));
    '
}

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
echo "==> Mode:     ${MODE}"
echo "==> Case:     ${CASE_ID}"
echo "==> Fixture:  ${FIXTURE_FILE}"
echo "==> Target:   ${GRPC_TARGET}"
echo "==> Endpoint: ${ENDPOINT}"
echo "==> space_id: ${SPACE_ID}"
echo
echo "--- request_body ---"
BODY="${REQUEST_BODY}" node -e "console.log(JSON.stringify(JSON.parse(process.env.BODY), null, 2))"
echo "--------------------"
echo

echo "==> Encrypting Authorization header..."
ENCRYPTED_HEADERS="$(encrypt_headers)"

echo "==> Building gRPC payload..."
GRPC_PAYLOAD="$(build_grpc_payload "${ENCRYPTED_HEADERS}")"

echo "==> Calling generative.LlmGenerateService/TestRemoteEndpoint ..."
echo

# Print the full grpcurl command for copy-paste reuse, with secrets redacted.
echo "--- grpcurl command (secrets redacted) ---"
PLAINTEXT_PREVIEW=()
[[ "${GRPC_PLAINTEXT:-0}" == "1" ]] && PLAINTEXT_PREVIEW=(-plaintext)
cat <<GRPCURL_CMD
grpcurl \\
    ${PLAINTEXT_PREVIEW[@]+-plaintext \\}
    -import-path "\${HOME}/arize/proto" \\
    -proto generative/llm_generate.proto \\
    -d '$(
        BODY="${GRPC_PAYLOAD}" node -e "
            const p = JSON.parse(process.env.BODY);
            p.encrypted_headers = '<REDACTED>';
            console.log(JSON.stringify(p));
        "
    )' \\
    ${GRPC_TARGET} \\
    generative.LlmGenerateService/TestRemoteEndpoint
GRPCURL_CMD
echo "------------------------------------------"
echo

# Run from the repo root so the relative `-import-path proto` resolves.
cd "${REPO_ROOT}"

# Local sidecar speaks plaintext gRPC; remote dev hosts terminate TLS. Use
# -plaintext only for localhost targets, TLS otherwise. Override with
# GRPC_PLAINTEXT=1 / GRPC_PLAINTEXT=0 if needed.
if [[ -z "${GRPC_PLAINTEXT:-}" ]]; then
    case "${GRPC_TARGET}" in
        localhost:*|127.0.0.1:*) GRPC_PLAINTEXT=1 ;;
        *) GRPC_PLAINTEXT=0 ;;
    esac
fi
PLAINTEXT_FLAG=()
[[ "${GRPC_PLAINTEXT}" == "1" ]] && PLAINTEXT_FLAG=(-plaintext)

# explicit proto (no server reflection on the sidecar).
grpcurl \
    ${PLAINTEXT_FLAG[@]+"${PLAINTEXT_FLAG[@]}"} \
    -import-path "${HOME}/arize/proto" \
    -proto generative/llm_generate.proto \
    -d "${GRPC_PAYLOAD}" \
    "${GRPC_TARGET}" \
    generative.LlmGenerateService/TestRemoteEndpoint
