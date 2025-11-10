thonimport logging
from typing import Any, Dict, List

from bs4 import BeautifulSoup

from utils.helpers import fetch_url, build_property_records

logger = logging.getLogger(__name__)

def parse(url: str, settings: Dict[str, Any]) -> List[Dict[str, Any]]:
    """
    Parse a Bayut listing URL and return structured property records.
    """
    html = fetch_url(url, settings)
    if not html:
        logger.warning("Empty HTML for %s", url)
        return []

    soup = BeautifulSoup(html, "lxml")
    records = build_property_records(
        soup=soup,
        url=url,
        portal="Bayut",
        settings=settings,
    )
    logger.debug("Bayut parsed %d records from %s", len(records), url)
    return records