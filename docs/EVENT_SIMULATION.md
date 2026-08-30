# Deterministic event-driven simulation

QuantSieve contains an offline event-driven execution kernel that shares the
same order state machine and exact paper ledger used by the durable paper OMS.
It has no network access and no path to a real account.

## Supported execution contract

- One paper account, one instrument, one quote currency, long-only.
- Market and limit orders.
- Signals created at a finalized bar close; eligibility begins at the next bar
  plus any configured latency.
- Opening gaps execute before limit touches inferred only from bar high/low.
- Orders at the same execution time consume the shared volume budget in
  deterministic intent-sequence order.
- Participation-rate limits and partial fills across bars.
- Explicit fees and deterministic slippage.
- Expiration, cancellation, cash rejection, and long-position rejection.
- Mandatory, immutable `OrderRiskRuleSet` and `KillSwitchSnapshot` inputs; there
  is no implicit allow policy. The config also carries the snapshot's explicit
  observation and availability times. The switch is checked when an intent is
  submitted, when the venue acknowledgement would occur, and immediately
  before every candidate fill. If it is engaged, unavailable, not yet
  available, or older than the configured maximum age at an execution
  boundary, the order is deterministically rejected or expired without a
  ledger mutation. This is still one run-wide snapshot, not a time-indexed
  operational kill-switch feed.
- A close-time `RiskDecision` built from the finalized bar close/hash, current
  exact ledger, and active-order reservations before any executable projection
  can exist. Buy reservations include the configured fee buffer, and the
  simulation fee rate cannot exceed that buffer.
- A direction-aware execution-time envelope: buy fills cannot exceed their
  approved upper price, sell fills cannot fall below their approved lower
  price, and economically favorable sell improvement is accepted. Notional,
  cash-floor, and available-long-position checks still apply independently.
- Exact `Decimal` economics, FIFO lots, realized P&L, and balanced journals.

Every fill is one `AtomicFillRecord` coupling an `OrderFillEvent` to the
matching `PaperFillEvent`. A hash-chained record stream rebuilds both the order
projection and ledger projection; replay fails closed if their identities,
economics, revisions, balances, submit-time risk decisions, reservations, or
execution-time envelopes diverge. Allowed decisions are carried unchanged by
`OrderSubmittedRecord` and `SimulatedOrderProjection`; a rejected submit has an
independent `OrderRiskRejectedRecord` and no executable projection.

Minimal usage:

```python
from quantsieve_engine import (
    SimulationBar,
    SimulationConfig,
    SubmitOrderIntent,
    simulate_event_driven,
    verify_event_simulation,
)

run = simulate_event_driven(config, bars, intents)
replay = verify_event_simulation(run)
```

Version-two receipts retain the canonical bars and intents alongside
`bars_hash` and `intents_hash`. `verify_event_simulation` validates those
content hashes, deterministically re-runs the simulator from the retained
inputs and immutable config/risk evidence, and compares the complete record
stream, orders, fills, ledger, and replay hashes. Re-signing an altered fill and
its downstream hashes therefore cannot make it pass verification. These
content-addressed receipts prove deterministic reproduction of the evidence
they contain; they are not a claim that an untrusted producer supplied
authentic exchange data, kill-switch state, or risk configuration.

`replay_simulation_records` is deliberately a lower-level diagnostic: it checks
the internal hash, order, ledger, and risk consistency of the record stream it
is given, but it has no canonical bars or intents. It must not be used as a
substitute for `verify_event_simulation`, which is the version-two full-run
verification boundary.

Legacy version-one hash-only receipts remain historical audit artifacts and
are not silently promoted to version two: without their original bars and
intents they cannot satisfy full deterministic verification.

## Not yet supported

- Shorting, borrowing, leverage, margin, futures funding, or liquidation.
- Multiple instruments/accounts or cross-currency cash.
- Stop, stop-limit, trailing, IOC, FOK, or post-only instructions.
- Tick/order-book queue simulation, stochastic fills, or venue cancel latency.
- Tick size, lot size, and other Instrument Master trading-rule enforcement.
- Splits, dividends, contract rolls, expiry, or other corporate/contract events.
- The durable Paper OMS now persists the same exact-Decimal per-order
  `RiskDecision` contract and rechecks its approved envelope at fill time.
  Integrating this offline fill engine with durable multi-session accounts is
  still separate roadmap work.
- A time-indexed kill-switch source or operational control plane. The current
  immutable snapshot is rechecked at execution boundaries, but it can only
  remain valid or become stale during a run; it cannot represent a later
  operator state transition.
- A broker, exchange, testnet, or live-order adapter.

The vectorized backtester remains the fast research path. This simulator is the
auditable execution foundation used to measure how bar-level order semantics
differ from vectorized assumptions; it is not evidence that a strategy will
trade at the modeled prices.
