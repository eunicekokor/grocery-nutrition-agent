# gRPC probe fixtures

Each `.json` file here is a complete `EvalEntityRequest` body
(`evals/remote_models.py`) used by `scripts/test_remote_endpoint.sh` as the
`request_body` sent through `generative.LlmGenerateService/TestRemoteEndpoint`.

## Usage

```bash
export BEARER_TOKEN="$REMOTE_EVAL_TOKEN"
export EXTERNAL_LLM_API_KEY_ENCRYPTION_KEY="<32-byte-key>"

# list available cases
./scripts/test_remote_endpoint.sh --list

# run a specific case
./scripts/test_remote_endpoint.sh 001_pass
./scripts/test_remote_endpoint.sh 021_avocado
```

## Cases


| Case id        | Golden row        | Evaluator              | Scenario                                                       | Expected judge outcome                                                                                                                                         |
| -------------- | ----------------- | ---------------------- | -------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `001_pass`     | 001 — rolled oats | `category_correctness` | Rich prediction with `MORE_OF` category                        | `label: no_ground_truth` — plumbing test; server passes `expected={}` so no ground truth reaches the judge                                                     |
| `003_halal`    | 003 — SPAM        | `dietary_safety`       | `personal_fit.dietary_conflicts` contains halal/pork conflict  | `label: pass` — no expected conflicts in `expected={}`, so judge returns trivially safe; demonstrates rich `personal_fit` payload shape                        |
| `021_avocado`  | 021 — avocado     | `allergen_recall`      | Empty ingredients; allergen is the product name itself         | `label: pass` — judge returns pass because `expected.profile.allergens` is empty (no profile on wire); payload demonstrates the product-name-allergen scenario |
| `022_almonds`  | 022 — almonds     | `allergen_recall`      | Product is a tree nut; `allergen_warnings` correctly populated | Same as above — `pass` because no profile in `expected={}`; demonstrates `allergen_warnings` present in `personal_fit`                                         |
| `unknown_eval` | —                 | `not_a_real_judge`     | Invalid evaluator name                                         | Remote endpoint returns HTTP 400; gRPC call succeeds but response body contains error detail                                                                   |
| `minimal`      | —                 | `category_correctness` | Smallest valid payload (required fields only)                  | `label: no_ground_truth` — verifies required-only payload is accepted                                                                                          |


## Important limitation

`POST /evals/v1/evaluate` currently passes `expected={}` to all judges, so
golden row fields (`expected_category`, `expected_dietary_conflicts`, `profile`)
are not forwarded from the fixture to the judge. Meaningful `category_correctness`
pass/fail scores and `allergen_recall` recall checks require a future update to
expose an `expected` field on `EvalEntityRequest`.

Until then, these probes validate:

- The gRPC encrypted-header + remote-endpoint wiring is functional
- The prediction payload shape is accepted and parsed correctly
- Error cases (unknown evaluator) surface the right HTTP status in the RPC response

## Adding a new fixture

1. Create `scripts/fixtures/<case_id>.json` following the schema in
  `evals/remote_models.py` (`EvalEntityRequest`).
2. Required fields: `request_id`, `evaluator`, `prediction`.
3. Add a row to this README.

