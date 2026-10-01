# v0.2.16
# { "Depends": "py-genlayer:1jb45aa8ynh2a9c9xn3b7qqh8sm5q93hwfp7jqmwsfhh8jpz09h6" }
"""
SLABond - bonded service-level agreements settled by validator re-measurement.

A provider (an AI agent, an API, any service) posts a bond. A consumer sets a
measurable promise ("uptime >= 99.5") pointing at a public metrics URL.
Anyone can ask for a compliance check. The leader fetches the metric; every
validator INDEPENDENTLY re-fetches it and only agrees if its own measurement is
within the agreement's tolerance AND lands in the same verdict band.
Penalties are paid out of the bond by deterministic code after consensus.

No LLM is needed: the non-determinism is the live web. The interesting part is
the tolerance-aware equivalence check, not the extraction.
"""

import json
from datetime import datetime, timezone

from genlayer import *

# ---- PURE-LOGIC-BEGIN ----
# Everything between these markers is plain Python (no `gl`, no storage) so it
# can be unit-tested with ordinary pytest. tests/test_pure_logic.py extracts
# and executes exactly this block.

SCALE = 1000  # metrics are handled as integer thousandths (99.5 -> 99500)

CLS_OK = "COMPLIANT"
CLS_BREACH = "BREACH"
CLS_GREY = "INCONCLUSIVE"
CLS_NODATA = "UNAVAILABLE"

NODATA_STREAK_LIMIT = 3  # consecutive UNAVAILABLE checks that count as a breach


def extract_metric_milli(body_text, path):
    """Read a number from JSON using a dotted path ("uptime.last_30d", "runs.0.ms").
    Returns integer thousandths, or None if anything is missing/invalid/negative."""
    try:
        node = json.loads(body_text)
        for part in path.split("."):
            if isinstance(node, list):
                node = node[int(part)]
            elif isinstance(node, dict):
                node = node[part]
            else:
                return None
        if isinstance(node, bool) or not isinstance(node, (int, float, str)):
            return None
        value = int(round(float(node) * SCALE))
        return value if value >= 0 else None
    except Exception:
        return None


def classify(value_milli, comparator, threshold_milli, tolerance_milli):
    """Turn a measurement into a verdict band.
    margin > +tolerance  -> COMPLIANT
    margin < -tolerance  -> BREACH
    otherwise            -> INCONCLUSIVE (grey zone: nobody is penalised)"""
    if value_milli is None:
        return CLS_NODATA
    if comparator == "gte":
        margin = value_milli - threshold_milli
    else:  # "lte"
        margin = threshold_milli - value_milli
    if margin > tolerance_milli:
        return CLS_OK
    if margin < -tolerance_milli:
        return CLS_BREACH
    return CLS_GREY


def equivalent(leader, mine, comparator, threshold_milli, tolerance_milli):
    """The validator's agreement rule.
    1. Both saw data, or both saw none.
    2. The two measurements are within `tolerance_milli` of each other.
    3. Both fall in the same verdict band.
    Anything malformed from the leader is rejected."""
    try:
        lv = leader["value_milli"]
        mv = mine["value_milli"]
    except Exception:
        return False
    if lv is None or mv is None:
        return lv is None and mv is None
    if not isinstance(lv, int) or isinstance(lv, bool):
        return False
    if abs(lv - mv) > tolerance_milli:
        return False
    return classify(lv, comparator, threshold_milli, tolerance_milli) == classify(
        mv, comparator, threshold_milli, tolerance_milli
    )


def decide(cls, breaches, max_breaches, nodata_streak, streak_limit=NODATA_STREAK_LIMIT):
    """State transition for one finished check.
    Returns (effect, new_breaches, new_streak, terminate)."""
    effect = "inconclusive"
    if cls == CLS_OK:
        effect, nodata_streak = "compliant", 0
    elif cls == CLS_GREY:
        effect, nodata_streak = "inconclusive", 0
    elif cls == CLS_BREACH:
        effect, nodata_streak = "breach", 0
    else:  # UNAVAILABLE: providers can't dodge by taking their metrics page down
        nodata_streak += 1
        if nodata_streak >= streak_limit:
            effect, nodata_streak = "breach", 0
        else:
            effect = "no_data"
    if effect == "breach":
        breaches += 1
    return effect, breaches, nodata_streak, breaches >= max_breaches


def reputation_score(compliant_checks, breaches):
    """0-1000. Breaches weigh 3x a compliant check. Neutral 500 with no history."""
    denom = compliant_checks + 3 * breaches
    if denom == 0:
        return 500
    return (1000 * compliant_checks) // denom


# ---- PURE-LOGIC-END ----


def _now() -> int:
    # GenVM pins the clock per transaction, so this is deterministic.
    return int(datetime.now(timezone.utc).timestamp())


def fetch_value_milli(url, path):
    """The non-deterministic part: live web read + deterministic extraction.
    Never raises; failures become None (= UNAVAILABLE)."""
    try:
        resp = gl.nondet.web.get(url)
        if getattr(resp, "status", 200) != 200:
            return None
        return extract_metric_milli(resp.body.decode("utf-8"), path)
    except Exception:
        return None


class SLABond(gl.Contract):
    # STATE
    #   next_id     - counter for agreement ids (starts at 1)
    #   agreements  - id (as string) -> JSON record, schema documented in README
    #   records     - provider address (lowercase hex) -> JSON track record
    next_id: u256
    agreements: TreeMap[str, str]
    records: TreeMap[str, str]

    def __init__(self):
        self.next_id = u256(1)

    # ---------- internal helpers ----------

    def _load(self, agreement_id: int) -> dict:
        raw = self.agreements.get(str(agreement_id))
        if raw is None:
            raise gl.vm.UserError("unknown agreement")
        return json.loads(raw)

    def _save(self, agreement_id: int, ag: dict):
        self.agreements[str(agreement_id)] = json.dumps(ag)

    def _record(self, provider_hex: str) -> dict:
        raw = self.records.get(provider_hex)
        if raw is None:
            return {"compliant": 0, "breaches": 0, "completed": 0, "terminated": 0, "paid_out": 0}
        return json.loads(raw)

    def _save_record(self, provider_hex: str, rec: dict):
        self.records[provider_hex] = json.dumps(rec)

    def _pay(self, to_hex: str, amount: int):
        if amount > 0:
            gl.get_contract_at(Address(to_hex)).emit_transfer(value=u256(amount))

    def _reject(self, sender: Address, amount: int, reason: str) -> dict:
        # In a payable method, `raise` would leave attached GEN stuck in the
        # contract, so refund first and report the reason instead.
        self._pay(sender.as_hex, amount)
        return {"ok": False, "reason": reason}

    # ---------- write methods ----------

    @gl.public.write
    def create_agreement(
        self,
        provider: Address,
        metrics_url: str,
        json_path: str,
        comparator: str,
        threshold_milli: int,
        tolerance_milli: int,
        penalty: int,
        max_breaches: int,
        interval_s: int,
        duration_s: int,
    ) -> int:
        """Called by the CONSUMER. Thresholds are in thousandths (99.5 -> 99500)."""
        consumer = gl.message.sender_address
        if consumer == provider:
            raise gl.vm.UserError("provider and consumer must differ")
        if comparator not in ("gte", "lte"):
            raise gl.vm.UserError("comparator must be 'gte' or 'lte'")
        if not metrics_url.startswith("http"):
            raise gl.vm.UserError("metrics_url must be http(s)")
        if min(threshold_milli, tolerance_milli, interval_s) < 0:
            raise gl.vm.UserError("numbers must be non-negative")
        if penalty <= 0 or max_breaches <= 0 or duration_s <= 0:
            raise gl.vm.UserError("penalty, max_breaches and duration_s must be > 0")

        aid = int(self.next_id)
        self.next_id = u256(aid + 1)
        self._save(
            aid,
            {
                "consumer": consumer.as_hex.lower(),
                "provider": provider.as_hex.lower(),
                "metrics_url": metrics_url,
                "json_path": json_path,
                "comparator": comparator,
                "threshold_milli": threshold_milli,
                "tolerance_milli": tolerance_milli,
                "penalty": penalty,
                "max_breaches": max_breaches,
                "interval_s": interval_s,
                "duration_s": duration_s,
                "status": "pending_bond",
                "bond": 0,
                "activated_at": 0,
                "last_check_at": 0,
                "checks": 0,
                "compliant_checks": 0,
                "breaches": 0,
                "nodata_streak": 0,
                "last_value_milli": None,
                "last_effect": "none",
            },
        )
        return aid

    @gl.public.write.payable
    def post_bond(self, agreement_id: int) -> dict:
        """Called by the PROVIDER, attaching GEN >= penalty * max_breaches."""
        sender = gl.message.sender_address
        amount = int(gl.message.value)
        raw = self.agreements.get(str(agreement_id))
        if raw is None:
            return self._reject(sender, amount, "unknown agreement")
        ag = json.loads(raw)
        if sender.as_hex.lower() != ag["provider"]:
            return self._reject(sender, amount, "only the provider can post the bond")
        if ag["status"] != "pending_bond":
            return self._reject(sender, amount, "agreement is not awaiting a bond")
        if amount < ag["penalty"] * ag["max_breaches"]:
            return self._reject(sender, amount, "bond must cover penalty * max_breaches")
        ag["bond"] = amount
        ag["status"] = "active"
        ag["activated_at"] = _now()
        self._save(agreement_id, ag)
        return {"ok": True, "bond": amount}

    @gl.public.write
    def check_compliance(self, agreement_id: int) -> dict:
        """Anyone may call. Runs one validator-verified measurement."""
        ag = self._load(agreement_id)
        if ag["status"] != "active":
            raise gl.vm.UserError("agreement is not active")
        now = _now()
        if now > ag["activated_at"] + ag["duration_s"]:
            raise gl.vm.UserError("term is over - call close_agreement")
        if ag["last_check_at"] and now - ag["last_check_at"] < ag["interval_s"]:
            raise gl.vm.UserError("too soon since the last check")

        # Copy to plain locals: nondet code must never touch contract storage.
        url = ag["metrics_url"]
        path = ag["json_path"]
        comparator = ag["comparator"]
        threshold = ag["threshold_milli"]
        tolerance = ag["tolerance_milli"]

        def leader_fn():
            return {"value_milli": fetch_value_milli(url, path)}

        def validator_fn(leader_result: gl.vm.Result) -> bool:
            if not isinstance(leader_result, gl.vm.Return):
                return False
            mine = leader_fn()  # independent re-measurement
            return equivalent(leader_result.calldata, mine, comparator, threshold, tolerance)

        measurement = gl.vm.run_nondet_unsafe(leader_fn, validator_fn)

        # ---- deterministic settlement, after consensus ----
        value = measurement["value_milli"]
        cls = classify(value, comparator, threshold, tolerance)
        effect, breaches, streak, terminate = decide(
            cls, ag["breaches"], ag["max_breaches"], ag["nodata_streak"]
        )
        provider_hex = ag["provider"]
        rec = self._record(provider_hex)

        ag["checks"] += 1
        ag["last_check_at"] = now
        ag["last_value_milli"] = value
        ag["last_effect"] = effect
        ag["nodata_streak"] = streak
        ag["breaches"] = breaches

        payout = 0
        if effect == "compliant":
            ag["compliant_checks"] += 1
            rec["compliant"] += 1
        elif effect == "breach":
            payout = min(ag["bond"], ag["penalty"])
            ag["bond"] -= payout
            rec["breaches"] += 1
            rec["paid_out"] += payout

        refund = 0
        if terminate:
            ag["status"] = "terminated"
            refund = ag["bond"]
            ag["bond"] = 0
            rec["terminated"] += 1

        # state first, transfers last
        self._save(agreement_id, ag)
        self._save_record(provider_hex, rec)
        self._pay(ag["consumer"], payout)
        self._pay(provider_hex, refund)
        return {"effect": effect, "value_milli": value, "payout": payout, "status": ag["status"]}

    @gl.public.write
    def close_agreement(self, agreement_id: int) -> dict:
        """After the term ends the unspent bond returns to the provider.
        The consumer may also cancel an agreement that never received a bond."""
        ag = self._load(agreement_id)
        sender = gl.message.sender_address.as_hex.lower()
        if ag["status"] == "pending_bond":
            if sender != ag["consumer"]:
                raise gl.vm.UserError("only the consumer can cancel an unfunded agreement")
            ag["status"] = "cancelled"
            self._save(agreement_id, ag)
            return {"status": "cancelled", "refund": 0}
        if ag["status"] != "active":
            raise gl.vm.UserError("agreement is already closed")
        if _now() <= ag["activated_at"] + ag["duration_s"]:
            raise gl.vm.UserError("term is not over yet")
        refund = ag["bond"]
        ag["bond"] = 0
        ag["status"] = "completed"
        rec = self._record(ag["provider"])
        rec["completed"] += 1
        self._save(agreement_id, ag)
        self._save_record(ag["provider"], rec)
        self._pay(ag["provider"], refund)
        return {"status": "completed", "refund": refund}

    # ---------- views ----------

    @gl.public.view
    def get_agreement(self, agreement_id: int) -> dict:
        return self._load(agreement_id)

    @gl.public.view
    def get_provider_record(self, provider: Address) -> dict:
        rec = self._record(provider.as_hex.lower())
        rec["score"] = reputation_score(rec["compliant"], rec["breaches"])
        return rec

    @gl.public.view
    def get_agreement_count(self) -> int:
        return int(self.next_id) - 1
