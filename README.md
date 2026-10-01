# SLABond

**Bonded service-level agreements for AI agents and APIs, settled by validator re-measurement.**
One standalone GenLayer Intelligent Contract. No frontend.

A provider posts a bond. A consumer writes a measurable promise ("uptime ≥ 99.5%", "p95 latency ≤ 200 ms") pointing at a public metrics URL. Anyone can trigger a compliance check. Validators independently re-measure the metric, and if they agree, deterministic code pays the consumer out of the provider's bond. Every outcome feeds a per-provider track record that lives in the same contract.

## Why this needs GenLayer

The promise is about the live web, and the live web changes between two reads. A normal smart contract can't fetch it; an oracle makes you trust one reporter. Here every validator fetches the page itself, and the contract defines what "agreeing" means for a number that drifts.

No LLM is involved on purpose. The non-determinism is the web, and the value of the contract is the equivalence rule, not a prompt.

## How consensus is used

`check_compliance` uses a custom leader/validator pair via `gl.vm.run_nondet_unsafe`:

- **Leader:** fetches `metrics_url`, extracts the number at `json_path`, returns `{"value_milli": int | None}`.
- **Each validator:** re-fetches the same URL on its own and accepts the leader only if all three hold:
  1. both saw data, or both saw none;
  2. the two measurements are within `tolerance_milli` of each other;
  3. both fall in the same verdict band.

**Verdict bands** (decided by the contract, not by anyone's opinion):

| Band | Meaning |
|---|---|
| COMPLIANT | better than the threshold by more than the tolerance |
| BREACH | worse than the threshold by more than the tolerance |
| INCONCLUSIVE | inside the tolerance band around the threshold: nobody is penalised |
| UNAVAILABLE | metrics endpoint unreachable or the path is missing |

Design choices worth reading:

- **The consensus object is the measurement, not the verdict.** The verdict is a pure function of the measurement and the agreement terms, computed after consensus. A leader can't smuggle in a verdict its number doesn't support.
- **Grey zone instead of coin-flips.** Near the threshold, two honest validators can land on opposite sides. The tolerance band makes that region a no-penalty zone, so consensus failures there are rare and harmless.
- **Withholding doesn't pay.** 3 consecutive UNAVAILABLE checks count as a breach, so a provider can't dodge by taking the metrics page down.
- **Nondeterministic code never touches storage.** Terms are copied to local variables before the nondet block; settlement happens after it.
- **State first, transfers last.** The payable method refunds instead of raising, so GEN can't get stuck.

## State design

```
next_id      u256                    counter, starts at 1
agreements   TreeMap[str, str]       "<id>" -> JSON record
records      TreeMap[str, str]       "<provider address, lowercase>" -> JSON record
```

Agreement record: `consumer, provider, metrics_url, json_path, comparator ("gte"|"lte"), threshold_milli, tolerance_milli, penalty, max_breaches, interval_s, duration_s, status, bond, activated_at, last_check_at, checks, compliant_checks, breaches, nodata_streak, last_value_milli, last_effect`

Status flow: `pending_bond → active → completed | terminated`, or `pending_bond → cancelled`.

Provider record: `compliant, breaches, completed, terminated, paid_out` plus a computed `score` (0–1000; a breach weighs 3× a compliant check; 500 with no history).

All metric numbers are integer **thousandths**: 99.5 is `99500`.

## Methods

| Method | Who | What |
|---|---|---|
| `create_agreement(provider, metrics_url, json_path, comparator, threshold_milli, tolerance_milli, penalty, max_breaches, interval_s, duration_s)` | consumer | creates the agreement, returns its id |
| `post_bond(agreement_id)` *(payable)* | provider | attach ≥ `penalty × max_breaches`; activates the agreement |
| `check_compliance(agreement_id)` | anyone | one validator-verified measurement; pays the penalty on a breach |
| `close_agreement(agreement_id)` | anyone after the term; consumer to cancel an unfunded one | returns the unspent bond to the provider |
| `get_agreement(id)`, `get_provider_record(addr)`, `get_agreement_count()` | view | |

When breaches reach `max_breaches` the agreement terminates and the leftover bond goes back to the provider.

## Try it (no coding needed)

**1. Publish a metrics file.** Go to gist.github.com, paste the contents of `examples/metrics.json`, create a **public** gist, click **Raw**, and copy that URL. That's your `metrics_url`. Edit the gist later to simulate good or bad service.

**2. Open GenLayer Studio** and pick the network you need (the hackathon network details are on the event page; Studio Next uses RPC `https://studio-next.genlayer.com/api`, chain ID `61997`, explorer `https://explorer-studio-dev.genlayer.com/`). Use the faucet to fund two accounts: one will be the consumer, one the provider.

**3. Deploy.** New Contract → Add From File → `contracts/sla_bond.py` → Deploy (no constructor arguments).

**4. As the consumer account,** call `create_agreement` with:

- `provider`: the provider account's address
- `metrics_url`: your raw gist URL
- `json_path`: `uptime.last_30d`
- `comparator`: `gte`
- `threshold_milli`: `99500`
- `tolerance_milli`: `100`
- `penalty`: `100`
- `max_breaches`: `3`
- `interval_s`: `0`
- `duration_s`: `86400`

**5. Switch to the provider account** and call `post_bond` with `agreement_id` `1`, attaching a value of at least `300`.

**6. Check compliance.** Call `check_compliance` with `1`. With the sample file (99.62) you get `compliant`. Now edit the gist so `last_30d` is `97.0` and call it again: validators re-fetch, agree, and the consumer receives `100` from the bond.

**7. Inspect:** `get_agreement(1)` and `get_provider_record(<provider address>)`.

CLI equivalent (`npm install -g genlayer`):

```
genlayer deploy --contract contracts/sla_bond.py
genlayer write $ADDR create_agreement --args addr#<PROVIDER> "<RAW_URL>" "uptime.last_30d" "gte" 99500 100 100 3 0 86400
genlayer write $ADDR post_bond --args 1        # run as provider, attach >= 300
genlayer write $ADDR check_compliance --args 1
genlayer call  $ADDR get_agreement --args 1
```

## Tests

`tests/test_pure_logic.py` is plain pytest (no Studio needed). It extracts the contract's pure-logic block (between the `PURE-LOGIC` markers) and tests exactly what gets deployed: extraction edge cases, band boundaries, every validator accept/reject path, the withholding rule, and the reputation formula. It also guards that the contract uses a custom validator and keeps web access in one place.

```
pip install pytest
pytest tests -v
```

## Limitations

- The metrics source is trusted to be hard to fake. The provider shouldn't control the page. Good sources: a third-party monitor's public JSON, a status API.
- Source must be JSON. Extending to unstructured status pages would be a natural place for an LLM extractor behind the same equivalence rule.
- Checks are pull-based: someone has to call `check_compliance`.
- The runner hash on line 2 of the contract must match one your target network accepts. If deployment complains about the header, copy the first two lines from one of Studio's example contracts.

## Ideas to extend

Multi-metric agreements, consumer-side appeal windows, a factory that deploys one contract per agreement, a shared reputation registry across many SLABond instances.
