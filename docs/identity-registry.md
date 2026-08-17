# Instrument identity registry

Identity must be matched on more than ticker. Every entry requires MOEX `SECID` and board plus
T-Invest UID, class code, ISIN, lot and API trading availability.

Verified on 2026-08-14 through the official read-only T-Invest MCP and public MOEX metadata:

| MOEX | Board | ISIN | T-Invest UID | Lot | API tradeable |
|---|---|---|---|---:|---|
| SBER | TQBR | RU0009029540 | e6123145-9665-43e0-8413-cd61b8aa9b13 | 1 | yes |

The registry is research input, not authorization to trade. Any mismatch in board, lot, UID or
availability must block execution until independently reconciled.
