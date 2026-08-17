from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from urllib.parse import urlparse


@dataclass(frozen=True, slots=True)
class OwnershipDisclosure:
    secid: str
    holder_name: str
    holder_type: str
    stake_percent: Decimal | None
    report_date: date
    published_at: datetime
    source_url: str
    source_kind: str
    verified: bool


def load_ownership_disclosures(
    path: Path, *, as_of: datetime, secid: str | None = None
) -> tuple[OwnershipDisclosure, ...]:
    if as_of.tzinfo is None:
        raise ValueError("as_of must be timezone-aware")
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, list):
        raise ValueError("ownership disclosure registry must be a list")
    result: list[OwnershipDisclosure] = []
    for item in raw:
        if not isinstance(item["verified"], bool):
            raise ValueError("ownership verified must be a JSON boolean")
        published_at = datetime.fromisoformat(str(item["published_at"]))
        if published_at.tzinfo is None or published_at > as_of:
            raise ValueError("ownership publication timestamp is invalid")
        source_url = str(item["source_url"])
        parsed = urlparse(source_url)
        if parsed.scheme != "https" or not parsed.netloc:
            raise ValueError("ownership source must be a direct HTTPS URL")
        stake = item.get("stake_percent")
        disclosure = OwnershipDisclosure(
            secid=str(item["secid"]),
            holder_name=str(item["holder_name"]),
            holder_type=str(item["holder_type"]),
            stake_percent=None if stake is None else Decimal(str(stake)),
            report_date=date.fromisoformat(str(item["report_date"])),
            published_at=published_at,
            source_url=source_url,
            source_kind=str(item["source_kind"]),
            verified=item["verified"],
        )
        if not disclosure.secid or not disclosure.holder_name or not disclosure.source_kind:
            raise ValueError("ownership identity and source_kind must not be empty")
        if disclosure.stake_percent is not None and not (
            Decimal("0") <= disclosure.stake_percent <= Decimal("100")
        ):
            raise ValueError("stake_percent must be in [0, 100]")
        if secid is None or disclosure.secid == secid:
            result.append(disclosure)
    return tuple(
        sorted(result, key=lambda item: (item.report_date, item.holder_name), reverse=True)
    )


def render_ownership_report(items: tuple[OwnershipDisclosure, ...], *, secid: str) -> str:
    lines = [f"Крупные держатели и фонды: {secid}"]
    verified = [item for item in items if item.verified]
    if not verified:
        lines.append("Нет актуального проверенного публичного раскрытия в локальном реестре.")
    for item in verified:
        stake = "доля не раскрыта" if item.stake_percent is None else f"{item.stake_percent}%"
        lines.append(
            f"- {item.holder_name} ({item.holder_type}): {stake}; "
            f"отчётная дата {item.report_date.isoformat()}; {item.source_url}"
        )
    lines.append("HI2/FUTOI не раскрывают имена участников; этот слой обновляется отдельно.")
    return "\n".join(lines)
