# Threat model

This document covers the self-hosted public edition. It is reviewed whenever a
new trust boundary, data provider, execution mode, or account integration is
introduced.

## Assets

- LLM/provider credentials supplied by the operator
- research databases, cached market data, experiments, and paper portfolios
- source citations and stored market-event text
- strategy code and generated research output
- integrity of backtest, optimization, and paper-execution results
- the operator's host, network, and browser session

## Trust boundaries

```text
browser
  -> web server / same-origin proxy
  -> API
      -> local databases
      -> strategy subprocess
      -> public data/news providers
      -> optional LLM provider
      -> optional private translation sidecar
```

Third-party market/news content, symbols, filings, model output, and user
strategy code are untrusted inputs. Local Docker networking is not by itself an
authentication or tenant-isolation mechanism.

## Principal threats and controls

| Threat | Current controls | Residual risk / required operator action |
| --- | --- | --- |
| Credential disclosure | Runtime environment variables, per-request BYOK, ignored secret files, public-release scanner | Browser local storage is unsuitable for shared machines; use a trusted profile and rotate any exposed key |
| Server/topology disclosure | Clean-history allowlist export; IP, host-path, secret and private-reference scanning | Never publish the development repository, a workspace archive, logs, databases, or release receipts |
| Untrusted strategy code | AST restrictions, separate process, timeout, resource limits where supported | Not a hostile multi-tenant sandbox; disable for untrusted users |
| SSRF or arbitrary translation proxying | Server-owned translation base URL; route accepts stored event identities instead of arbitrary text/URL | Keep the sidecar on a private network and restrict API egress |
| Prompt/content injection | Tool results are structured; cited market values are checked before final output | LLM narrative remains untrusted analysis; never turn news/model text directly into an order |
| Data poisoning, stale or revised data | Source metadata, finalized-bar filter, OHLCV checks, explicit provider status | Public sources can still be wrong; verify critical evidence and use versioned datasets as roadmap work lands |
| Backtest overfitting or look-ahead | Next-bar execution, warm-up isolation, development/validation/final holdout, cost stress | A passing backtest is evidence, not a profitability guarantee |
| Unauthorized network access or Paper OMS mutation | Compose binds to loopback by default; explicit CORS configuration; the persistent Paper OMS kill switch starts engaged and has no public clear endpoint | All REST routes still lack application authentication/RBAC; keep them on a controlled private boundary and add an authenticated TLS reverse proxy before remote access |
| Dependency/build compromise | Locked JavaScript dependencies, constrained Python runtime packages, pinned CI actions, dependency review | Python development dependencies and base images still need stronger reproducible pinning and SBOM/provenance |
| Uncontrolled real orders | Public edition has no real-account order path | Future live adapters require a separate credential vault, risk kernel, approval, OMS, reconciliation and kill switch |

## Non-goals in the current version

- hostile multi-user code execution
- custody of exchange or brokerage credentials
- authorization for live trading
- Internet-facing deployment without an external authentication layer
- guaranteed correctness or availability of third-party data

## Security invariants for future live execution

Live connectivity must not be enabled until all of the following are testable:

1. Every order has an idempotency key and a complete state transition history.
2. A shared pre-trade risk kernel can reject stale data, excess notional,
   concentration, leverage, loss, or operator-defined limits.
3. A global kill switch fails closed independently of strategy code.
4. Cash, positions, fills, fees, funding and corporate actions reconcile
   against the venue.
5. Credentials are encrypted, scoped, rotatable, never browser-readable, and
   never logged.
6. Audit events, alerts, backup restoration and deterministic replay are
   exercised before real capital is connected.
