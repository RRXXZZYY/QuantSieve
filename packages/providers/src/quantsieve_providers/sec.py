from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta
from typing import Any

import httpx

from .cache import SQLiteCache
from .models import Citation, DataEnvelope

SEC_SUBMISSIONS = "https://data.sec.gov/submissions"
SEC_ARCHIVES = "https://www.sec.gov/Archives/edgar/data"


class SECEdgarProvider:
    """Read 13F holdings from official SEC EDGAR submissions and archives."""

    name = "sec-edgar"

    def __init__(
        self,
        user_agent: str,
        cache: SQLiteCache | None = None,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        if not user_agent.strip():
            raise ValueError("SEC requests require a descriptive User-Agent with contact details.")
        self.user_agent = user_agent
        self.cache = cache or SQLiteCache()
        self._client = client

    async def latest_13f(self, cik: str, institution: str | None = None) -> DataEnvelope:
        cik_padded = str(cik).zfill(10)
        key = self.cache.make_key("sec:13f", cik=cik_padded)
        cached = self.cache.get(key)
        if cached is not None:
            return DataEnvelope.model_validate(cached)

        submissions_url = f"{SEC_SUBMISSIONS}/CIK{cik_padded}.json"
        submissions = await self._get_json(submissions_url)
        recent = submissions["filings"]["recent"]
        index = next(
            (position for position, form in enumerate(recent["form"]) if form == "13F-HR"),
            None,
        )
        if index is None:
            raise LookupError(f"No recent 13F-HR filing found for CIK {cik_padded}.")

        accession = recent["accessionNumber"][index]
        filed_at = recent["filingDate"][index]
        primary_document = recent["primaryDocument"][index]
        accession_path = accession.replace("-", "")
        cik_path = str(int(cik_padded))
        archive_base = f"{SEC_ARCHIVES}/{cik_path}/{accession_path}"
        filing_index = await self._get_json(f"{archive_base}/index.json")
        items = filing_index["directory"]["item"]
        xml_candidates = [
            item["name"]
            for item in items
            if item["name"].lower().endswith(".xml")
            and item["name"].lower() != primary_document.lower()
            and not item["name"].lower().startswith(("primary_doc", "form13f"))
        ]
        if not xml_candidates:
            xml_candidates = [
                item["name"]
                for item in items
                if item["name"].lower().endswith(".xml")
                and item["name"].lower() != primary_document.lower()
            ]
        if not xml_candidates:
            raise LookupError(f"No 13F information-table XML found in filing {accession}.")

        information_url = f"{archive_base}/{xml_candidates[0]}"
        xml_text = await self._get_text(information_url)
        rows = self._parse_information_table(xml_text)
        envelope = DataEnvelope(
            symbol=cik_padded,
            kind="13f_holdings",
            rows=rows,
            citations=[
                Citation(
                    source="SEC EDGAR",
                    url=f"{archive_base}/{primary_document}",
                    as_of=datetime.fromisoformat(f"{filed_at}T00:00:00+00:00"),
                    note=f"Official 13F-HR filing {accession}",
                )
            ],
            metadata={
                "institution": institution or submissions.get("name"),
                "cik": cik_padded,
                "accession_number": accession,
                "filing_date": filed_at,
                "information_table_url": information_url,
            },
        )
        self.cache.set(key, envelope.model_dump(mode="json"), timedelta(hours=12))
        return envelope

    async def _get_json(self, url: str) -> dict[str, Any]:
        client = self._client or httpx.AsyncClient(timeout=30)
        try:
            response = await client.get(
                url,
                headers={
                    "User-Agent": self.user_agent,
                    "Accept-Encoding": "gzip, deflate",
                    "Host": httpx.URL(url).host,
                },
            )
            response.raise_for_status()
            return dict(response.json())
        finally:
            if self._client is None:
                await client.aclose()

    async def _get_text(self, url: str) -> str:
        client = self._client or httpx.AsyncClient(timeout=30)
        try:
            response = await client.get(url, headers={"User-Agent": self.user_agent})
            response.raise_for_status()
            return response.text
        finally:
            if self._client is None:
                await client.aclose()

    @staticmethod
    def _parse_information_table(xml_text: str) -> list[dict[str, Any]]:
        root = ET.fromstring(xml_text)

        def text(element: ET.Element, local_name: str) -> str | None:
            child = next(
                (node for node in element.iter() if node.tag.rsplit("}", 1)[-1] == local_name),
                None,
            )
            return child.text.strip() if child is not None and child.text else None

        rows: list[dict[str, Any]] = []
        for table in (node for node in root.iter() if node.tag.rsplit("}", 1)[-1] == "infoTable"):
            value_thousands = text(table, "value")
            shares = text(table, "sshPrnamt")
            rows.append(
                {
                    "issuer": text(table, "nameOfIssuer"),
                    "class": text(table, "titleOfClass"),
                    "cusip": text(table, "cusip"),
                    "value_usd": int(re.sub(r"\D", "", value_thousands or "0")) * 1000,
                    "shares": int(re.sub(r"\D", "", shares or "0")),
                    "share_type": text(table, "sshPrnamtType"),
                    "investment_discretion": text(table, "investmentDiscretion"),
                }
            )
        return rows
