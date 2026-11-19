"""Source credibility tiers (config/sources_v1.json)."""
from __future__ import annotations

from urllib.parse import urlsplit

from .settings import load_config

TYPE_FOR_TIER = {"1": "company_site", "2": "publication", "3": "third_party", "lead": "search_lead"}
LABEL = {"company_site": "company site", "job_board": "job board (linked from company site)",
         "publication": "credible press", "third_party": "third-party database", "search_lead": "search lead"}


def host_of(url: str) -> str:
    return (urlsplit(url if "://" in url else "https://" + url).hostname or "").lower().removeprefix("www.")


def _matches(host: str, domains: list[str]) -> bool:
    return any(host == d or host.endswith("." + d) for d in domains)


def never_fetch(url: str) -> bool:
    return _matches(host_of(url), load_config("sources_v1")["never_fetch"])


def tier(url: str, company_domain: str, simulation: bool = False) -> str:
    """'1' company's own site · '2' credible press · '3' third-party database · 'lead' anything else."""
    host = host_of(url)
    if host == company_domain or host.endswith("." + company_domain):
        return "1"
    cfg = load_config("sources_v1")["tiers"]
    if _matches(host, cfg["2"]):
        return "2"
    if _matches(host, cfg["3"]):
        return "3"
    if simulation and host.endswith(".example"):  # fake press in simulation fixtures
        return "2"
    return "lead"
