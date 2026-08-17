# Global control-practice mapping

The harness applies established control themes, adapted for a private research bot rather than claiming regulatory compliance.

| Practice | Harness implementation |
|---|---|
| Pre-trade financial exposure controls | Independent `risk.py`, cash-only MVP, position/order/turnover/open-order limits |
| Prevent erroneous or excessive orders | Tick/lot validation, max notional, deterministic idempotency key, state machine |
| Independent control from strategy | Strategy emits weights only; execution requires the risk gate |
| Test before production | Unit, contract, replay, fault, sandbox, shadow and restricted-live gates |
| Review after changes | CI, immutable configuration, planned release checklist and audit log |
| Operational resilience | Fail closed, reconciliation, circuit-breaker design, recovery drills |
| Auditability | Versioned inputs/config, JSONL decision trail and deterministic replay |
| Human control | Live disabled, explicit interlock design and manual recovery approval |
| AI safety | Geo/LLM output can only reduce risk; no broker credential or order capability |

Primary references:

- FINRA algorithmic trading supervision and system validation: https://www.finra.org/rules-guidance/key-topics/algorithmic-trading
- SEC market-access pre-trade controls (used as a control-design reference): https://www.sec.gov/files/rules/final/2010/34-63241fr.pdf
- Basel Committee operational resilience principles: https://www.bis.org/bcbs/publ/d516.htm
- T-Invest idempotency and stream lifecycle: https://developer.tbank.ru/invest/intro/developer/sla
- T-Invest sandbox limitations: https://developer.tbank.ru/invest/intro/developer/sandbox

