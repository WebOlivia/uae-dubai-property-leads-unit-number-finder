thonimport argparse
import logging
import sys
from pathlib import Path
from typing import Any, Dict, List
from urllib.parse import urlparse

from utils.helpers import (
    load_json_file,
    export_results,
    setup_logging,
    load_settings,
)

from parsers.propertyfinder_parser import parse as parse_propertyfinder
from parsers.bayut_parser import parse as parse_bayut
from parsers.dubizzle_parser import parse as parse_dubizzle

logger = logging.getLogger(__name__)

SUPPORTED_PORTALS = {
    "propertyfinder": parse_propertyfinder,
    "bayut": parse_bayut,
    "dubizzle": parse_dubizzle,
}

def detect_portal(url: str) -> str:
    """
    Detect which portal the URL belongs to based on its hostname.
    """
    netloc = urlparse(url).netloc.lower()
    if "propertyfinder" in netloc:
        return "propertyfinder"
    if "bayut" in netloc:
        return "bayut"
    if "dubizzle" in netloc:
        return "dubizzle"
    return ""

def normalize_input_urls(raw: Any) -> List[Dict[str, Any]]:
    """
    Accepts:
      - list[str]
      - list[{"url": "..."}]
    Returns list of {"url": "...", "meta": {...}}
    """
    normalized: List[Dict[str, Any]] = []

    if isinstance(raw, list):
        for item in raw:
            if isinstance(item, str):
                normalized.append({"url": item, "meta": {}})
            elif isinstance(item, dict) and "url" in item:
                meta = {k: v for k, v in item.items() if k != "url"}
                normalized.append({"url": item["url"], "meta": meta})
            else:
                logger.warning("Skipping unsupported input item: %r", item)
    else:
        logger.error("Input URLs JSON must be a list of strings or objects.")
        raise ValueError("Unsupported input_urls.json format")

    return normalized

def process_url(
    url: str,
    settings: Dict[str, Any],
    meta: Dict[str, Any] | None = None,
) -> List[Dict[str, Any]]:
    """
    Route the URL to the correct parser and return extracted records.
    """
    portal = detect_portal(url)
    if not portal:
        logger.warning("Unsupported portal for URL %s; skipping.", url)
        return []

    parser_func = SUPPORTED_PORTALS.get(portal)
    if not parser_func:
        logger.warning("No parser configured for portal %s; skipping %s.", portal, url)
        return []

    try:
        records = parser_func(url, settings)
        # Attach meta and portal info to each record
        for record in records:
            record.setdefault("SourceURL", url)
            record.setdefault("Portal", portal)
            if meta:
                record.setdefault("Meta", meta)
        logger.info("Processed %s records from %s", len(records), url)
        return records
    except Exception as exc:  # noqa: BLE001
        logger.exception("Failed to process URL %s: %s", url, exc)
        return []

def parse_args(argv: List[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="UAE Dubai Property Leads Unit Number Finder Scraper",
    )
    parser.add_argument(
        "--input",
        "-i",
        default="data/input_urls.json",
        help="Path to input URLs JSON file",
    )
    parser.add_argument(
        "--output",
        "-o",
        default="data/sample_output.json",
        help="Path to output file (format inferred from extension if --format not set)",
    )
    parser.add_argument(
        "--format",
        "-f",
        choices=["json", "csv", "xlsx", "xml", "html"],
        help="Output format (overrides settings.json default and file extension)",
    )
    parser.add_argument(
        "--retrieve-contact-details",
        dest="retrieve_contact_details",
        action="store_true",
        help="Enable scraping of owner phone/email when available",
    )
    parser.add_argument(
        "--no-retrieve-contact-details",
        dest="retrieve_contact_details",
        action="store_false",
        help="Disable scraping of owner phone/email",
    )
    parser.set_defaults(retrieve_contact_details=None)
    return parser.parse_args(argv)

def main(argv: List[str] | None = None) -> int:
    if argv is None:
        argv = sys.argv[1:]

    setup_logging()
    args = parse_args(argv)

    project_root = Path(__file__).resolve().parents[1]
    settings_path = project_root / "src" / "config" / "settings.json"
    settings = load_settings(settings_path)

    # Override contact settings from CLI if specified
    if args.retrieve_contact_details is not None:
        settings.setdefault("scraper", {})
        settings["scraper"]["retrieve_contact_details"] = bool(
            args.retrieve_contact_details
        )