thonimport csv
import json
import logging
import os
import re
from pathlib import Path
from typing import Any, Dict, Iterable, List

import pandas as pd
import requests
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (compatible; UAEPropertyScraper/1.0; +https://bitbash.dev)"
)

def setup_logging(level: int = logging.INFO) -> None:
    if logging.getLogger().handlers:
        # Already configured
        return
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)s] %(name)s - %(message)s",
    )

def load_settings(path: Path) -> Dict[str, Any]:
    """
    Load settings.json from disk. If it does not exist, return sane defaults.
    """
    if not path.exists():
        logger.warning("settings.json not found at %s; using defaults.", path)
        return {
            "scraper": {
                "user_agent": DEFAULT_USER_AGENT,
                "timeout": 15,
                "retrieve_contact_details": False,
            },
            "output": {
                "default_format": "json",
            },
        }

    with path.open("r", encoding="utf-8") as f:
        try:
            return json.load(f)
        except json.JSONDecodeError as exc:
            logger.error("Failed to parse settings.json: %s", exc)
            raise

def load_json_file(path: Path) -> Any:
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)

def ensure_parent_dir(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)

def export_results(records: Iterable[Dict[str, Any]], output_path: Path, fmt: str) -> None:
    records_list = list(records)
    ensure_parent_dir(output_path)

    fmt = fmt.lower()
    if fmt == "json":
        with output_path.open("w", encoding="utf-8") as f:
            json.dump(records_list, f, ensure_ascii=False, indent=2, default=str)
        return

    if fmt == "csv":
        if not records_list:
            # Create an empty file with header comment
            with output_path.open("w", encoding="utf-8", newline="") as f:
                f.write("# No records to export\n")
            return
        fieldnames = sorted({k for r in records_list for k in r.keys()})
        with output_path.open("w", encoding="utf-8", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            for row in records_list:
                writer.writerow(row)
        return

    # For remaining formats, use pandas
    df = pd.DataFrame(records_list)

    if fmt == "xlsx":
        df.to_excel(output_path, index=False)
    elif fmt == "xml":
        # Simple XML representation
        from xml.etree.ElementTree import Element, SubElement, ElementTree

        root = Element("Properties")
        for record in records_list:
            rec_el = SubElement(root, "Property")
            for k, v in record.items():
                child = SubElement(rec_el, k)
                child.text = "" if v is None else str(v)

        tree = ElementTree(root)
        tree.write(output_path, encoding="utf-8", xml_declaration=True)
    elif fmt == "html":
        df.to_html(output_path, index=False)
    else:
        raise ValueError(f"Unsupported export format: {fmt}")

def build_headers(settings: Dict[str, Any]) -> Dict[str, str]:
    ua = (
        settings.get("scraper", {})
        .get("user_agent", DEFAULT_USER_AGENT)
    )
    return {
        "User-Agent": ua,
        "Accept-Language": "en-US,en;q=0.9",
    }

def fetch_url(url: str, settings: Dict[str, Any]) -> str:
    """
    Fetch the HTML for a URL using requests with sensible defaults and error handling.
    """
    headers = build_headers(settings)
    timeout = (
        settings.get("scraper", {})
        .get("timeout", 15)
    )

    try:
        with requests.Session() as session:
            resp = session.get(url, headers=headers, timeout=timeout)
            resp.raise_for_status()
            return resp.text
    except requests.RequestException as exc:
        logger.error("HTTP error while fetching %s: %s", url, exc)
        return ""

def _parse_json_maybe(text: str) -> List[Dict[str, Any]]:
    """
    Try parsing a JSON or JSON array which may contain multiple objects.
    """
    text = text.strip()
    if not text:
        return []
    try:
        data = json.loads(text)
        if isinstance(data, dict):
            return [data]
        if isinstance(data, list):
            # Filter dict-like entries
            return [d for d in data if isinstance(d, dict)]
        return []
    except json.JSONDecodeError:
        return []

def extract_json_ld(soup: BeautifulSoup) -> List[Dict[str, Any]]:
    """
    Extract JSON-LD blobs from the page and return them as a list of dicts.
    """
    results: List[Dict[str, Any]] = []
    for script in soup.find_all("script", attrs={"type": "application/ld+json"}):
        blobs = _parse_json_maybe(script.string or script.text or "")
        results.extend(blobs)
    return results

def _first_non_empty(*values: Any) -> Any:
    for v in values:
        if v not in (None, "", [], {}, ()):
            return v
    return None

def _extract_contact_details(soup: BeautifulSoup) -> Dict[str, Any]:
    """
    Try to find phone numbers and emails in tel: / mailto: links.
    """
    phones: set[str] = set()
    emails: set[str] = set()

    for a in soup.find_all("a", href=True):
        href = a["href"]
        if href.startswith("tel:"):
            phones.add(href.split(":", 1)[1].strip())
        elif href.startswith("mailto:"):
            emails.add(href.split(":", 1)[1].strip())

    return {
        "OwnerPhones": sorted(phones) if phones else None,
        "OwnerEmails": sorted(emails) if emails else None,
    }

def _extract_unit_number_from_text(text: str) -> str | None:
    """
    Try to infer unit number from free text using simple patterns.
    Examples:
      - "Unit 908"
      - "Apartment 908"
      - "Flat #908"
    """
    patterns = [
        r"\bUnit\s+([A-Za-z0-9\-]+)\b",
        r"\bApt(?:\.|artment)?\s+([A-Za-z0-9\-]+)\b",
        r"\bFlat\s*#?\s*([A-Za-z0-9\-]+)\b",
        r"\bOffice\s+([A-Za-z0-9\-]+)\b",
    ]
    for pattern in patterns:
        m = re.search(pattern, text, flags=re.IGNORECASE)
        if m:
            return m.group(1)
    return None

def build_property_records(
    soup: BeautifulSoup,
    url: str,
    portal: str,
    settings: Dict[str, Any],
) -> List[Dict[str, Any]]:
    """
    Build one or more property records from a listing page, attempting to use
    JSON-LD as the primary data source and then falling back to HTML content.
    """
    json_ld_objects = extract_json_ld(soup)

    candidate: Dict[str, Any] | None = None
    # Prefer objects that look like property / offer entities
    preferred_types = {
        "Apartment",
        "Unit",
        "Product",
        "Offer",
        "SingleFamilyResidence",
        "House",
        "Residence",
        "RealEstateListing",
    }

    for obj in json_ld_objects:
        obj_type = obj.get("@type")
        if isinstance(obj_type, list):
            types = set(obj_type)
        else:
            types = {obj_type} if obj_type else set()
        if types & preferred_types:
            candidate = obj
            break

    # Fallback to first object if nothing matched
    if candidate is None and json_ld_objects:
        candidate = json_ld_objects[0]

    text_content = soup.get_text(separator=" ", strip=True)[:5000]

    authority_name_en = None
    authority_name_ar = None
    permit_number = None
    permit_end_date = None
    listing_number = None
    real_estate_number = None
    property_name_en = None
    zone_name_en = None
    property_type_name_en = None
    property_value = None
    rooms_count = None
    property_size = None
    building_name_en = None
    unit_number = None
    license_number = None
    developer_name_en = None
    permit_type_name_en = None

    if candidate:
        # Many portals nest data in "offers", "seller", "address", etc.
        offers = candidate.get("offers") if isinstance(candidate.get("offers"), dict) else None
        address = candidate.get("address") if isinstance(candidate.get("address"), dict) else None
        seller = candidate.get("seller") if isinstance(candidate.get("seller"), dict) else None
        broker = candidate.get("broker") if isinstance(candidate.get("broker"), dict) else None

        authority_name_en = _first_non_empty(
            seller.get("name") if seller else None,
            broker.get("name") if broker else None,
        )
        property_name_en = _first_non_empty(
            candidate.get("name"),
            candidate.get("headline"),
        )
        zone_name_en = _first_non_empty(
            address.get("addressLocality") if address else None,
            address.get("streetAddress") if address else None,
        )
        property_type_name_en = _first_non_empty(
            candidate.get("@type"),
            candidate.get("propertyType"),
        )
        property_value = _first_non_empty(
            (offers or {}).get("price") if offers else None,
            candidate.get("price"),
        )
        rooms_count = _first_non_empty(
            candidate.get("numberOfRooms"),
            candidate.get("rooms"),
        )
        property_size = _first_non_empty(
            (candidate.get("floorSize") or {}).get("value")
            if isinstance(candidate.get("floorSize"), dict)
            else None,
            candidate.get("area"),
        )
        building_name_en = _first_non_empty(
            candidate.get("buildingName"),
            candidate.get("project"),
            candidate.get("name"),
        )
        listing_number = _first_non_empty(
            candidate.get("sku"),
            candidate.get("listingId"),
            candidate.get("identifier"),
        )
        permit_type_name_en = _first_non_empty(
            candidate.get("category"),
            candidate.get("availability"),
        )
        developer_name_en = _first_non_empty(
            (candidate.get("developer") or {}).get("name")
            if isinstance(candidate.get("developer"), dict)
            else None,
        )

    # Try to infer a unit number from text if not already determined
    unit_number = _first_non_empty(
        unit_number,
        _extract_unit_number_from_text(text_content),
    )

    # Fallbacks using the DOM directly
    if not property_name_en:
        title_tag = soup.find("title")
        if title_tag and title_tag.string:
            property_name_en = title_tag.string.strip()

    if not property_type_name_en:
        # Heuristic: look for breadcrumb or category labels
        breadcrumb = soup.find("nav", {"aria-label": "breadcrumb"})
        if breadcrumb:
            items = [li.get_text(strip=True) for li in breadcrumb.find_all("li")]
            if items:
                property_type_name_en = items[-1]

    # Contact details if enabled
    contact_info: Dict[str, Any] = {}
    retrieve_contact = (
        settings.get("scraper", {})
        .get("retrieve_contact_details", False)
    )
    if retrieve_contact:
        contact_info = _extract_contact_details(soup)

    record: Dict[str, Any] = {
        "AuthorityNameEn": authority_name_en,
        "AuthorityNameAr": authority_name_ar,
        "PermitNumber": permit_number,
        "PermitEndDate": permit_end_date,
        "ListingNumber": listing_number,
        "RealEstateNumber": real_estate_number,
        "PropertyNameEn": property_name_en,
        "ZoneNameEn": zone_name_en,
        "PropertyTypeNameEn": property_type_name_en,
        "PropertyValue": property_value,
        "RoomsCount": rooms_count,
        "PropertySize": property_size,
        "BuildingNameEn": building_name_en,
        "UnitNumber": unit_number,
        "LicenseNumber": license_number,
        "DeveloperNameEn": developer_name_en,
        "PermitTypeNameEn": permit_type_name_en,
    }

    # Attach contact info fields
    record.update(contact_info)

    # Always return a list so that future extension (multi-listing pages) is easy
    return [record]