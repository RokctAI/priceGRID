"""
Catalogue access via the storefront's own JSON API.

The retailer rebuilt its storefront as a Next.js app during 2026. The old
``/c-<id>/<Name>`` category pages and their server-rendered product markup are
gone: those URLs now 308 to the homepage, so every selector the HTML scraper
relies on matches nothing. The replacement front end reads its data from
``POST /api/catalogue/get-products-filter``, which returns the whole product
record as JSON - name, price, promotion state, brand, barcodes, descriptions
and image URLs - with no markup to parse.

The API is not open to plain HTTP clients. AWS WAF fronts the site and issues a
JavaScript challenge, so curl and requests get 403 while a real browser gets
200; this is a challenge the client cannot solve, not a block on the caller's
address. Every call here is therefore issued from inside a Playwright page with
``fetch``, which carries the WAF token the browser has already earned along
with the site's own cookies and headers.
"""

import json
import logging
import re
from html import unescape
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# The two storefronts run the same front end: identical routes, identical
# endpoints, different catalogue behind them. One base URL is the only
# difference between scraping them.
STORES = {
    "shoprite": "https://www.shoprite.co.za",
    "checkers": "https://www.checkers.co.za",
}

BASE_URL = STORES["shoprite"]
PRODUCTS_ENDPOINT = "/api/catalogue/get-products-filter"
CATEGORY_TREE_ENDPOINT = "/api/catalogue/get-category-tree"


def set_store(store: str) -> str:
    """Point this module at one of the STORES. Returns the base URL chosen."""
    global BASE_URL
    if store not in STORES:
        raise ValueError(f"Unknown store {store!r}; expected one of {sorted(STORES)}.")
    BASE_URL = STORES[store]
    return BASE_URL


# Department links look like /department/<section>/<name>-<level>-<id>, e.g.
# /department/bakery/pies-2-670437ec7a8738098af92e2b. The trailing 24-character
# hex id and the level are what the API wants; the words before them are for
# humans and for the URL alone.
DEPARTMENT_RE = re.compile(
    r"^/department/(?P<section>[^/]+)/(?P<slug>.+?)-(?P<level>\d+)-(?P<id>[0-9a-f]{24})$"
)


class Department:
    """One browsable category, as the nav and the API both describe it."""

    def __init__(
        self,
        section: str,
        slug: str,
        level: int,
        category_id: str,
        name: Optional[str] = None,
    ):
        self.section = section
        self.slug = slug
        self.level = level
        self.category_id = category_id
        # The tree gives the real display name. Recovering one from the slug is
        # only a fallback for a Department parsed out of a URL, and it is lossy:
        # "Airtime, Data &  Vouchers" does not survive the round trip.
        self._name = name

    @property
    def name(self) -> str:
        """The display name the API expects."""
        if self._name:
            return self._name
        return self.slug.replace("-", " ").title()

    @property
    def url(self) -> str:
        return f"{BASE_URL}/department/{self.section}/{self.slug}-{self.level}-{self.category_id}"

    def __repr__(self) -> str:
        return f"<Department {self.section}/{self.slug} id={self.category_id}>"


def slugify(text: str) -> str:
    """A URL-safe slug, used for the display paths departments are logged under."""
    return re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")


def parse_department_url(href: str) -> Optional[Department]:
    """Turn a /department/... link into a Department, or None if it is not one."""
    match = DEPARTMENT_RE.match(href.split("?")[0].rstrip("/"))
    if not match:
        return None
    return Department(
        section=match.group("section"),
        slug=match.group("slug"),
        level=int(match.group("level")),
        category_id=match.group("id"),
    )


async def discover_departments(page) -> List[Department]:
    """
    Every department, read from the category tree the storefront publishes.

    ``POST /api/catalogue/get-category-tree`` with an empty body returns the
    whole tree - the same data the "Shop by Department" menu renders - as nodes
    carrying the id, name and level that fetch_products needs. Reading it here
    rather than clicking the menu open and scraping its links matters: on a CI
    runner the menu never rendered within fifteen seconds and discovery came
    back empty, failing the run on a site that was answering perfectly well.

    Only level-2 nodes are returned. Level 1 is the aisle heading, which holds
    no products of its own.
    """
    await page.goto(BASE_URL, wait_until="domcontentloaded", timeout=90000)

    # Allow Shoprite's browser/WAF JavaScript challenge to complete.
    try:
        await page.wait_for_load_state("networkidle", timeout=30000)
    except Exception:
        logger.info("Shoprite did not reach networkidle; continuing.")

    await page.wait_for_timeout(5000)
    result = await page.evaluate(
        """async (endpoint) => {
            const res = await fetch(endpoint, {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: '{}',
                credentials: 'include',
            });
            const body = await res.text();
            const headers = {};
            for (const [key, value] of res.headers.entries()) {
                headers[key] = value;
            }
            return {status: res.status, body: body, headers: headers};
        }""",
        CATEGORY_TREE_ENDPOINT,
    )

    if result["status"] != 200:
        logger.error(
            "Shoprite API response: status=%s headers=%s body=%r",
            result["status"],
            result.get("headers"),
            result.get("body", "")[:2000],
        )
        raise RuntimeError(
            f"Category tree returned {result['status']}; the catalogue could not "
            "be read."
        )

    tree = json.loads(result["body"]).get("displayCategoryTree") or []

    departments: Dict[str, Department] = {}
    for section in tree:
        section_slug = slugify(section.get("name") or "")
        for child in section.get("displayCategories") or []:
            if child.get("level") != 2 or not child.get("id"):
                continue
            departments[child["id"]] = Department(
                section=section_slug,
                slug=slugify(child.get("name") or ""),
                level=2,
                category_id=child["id"],
                name=child.get("name"),
            )

    logger.info(f"Discovered {len(departments)} departments.")
    return list(departments.values())


async def fetch_products(
    page, department: Department, page_no: int = 0, page_size: int = 50
) -> Dict[str, Any]:
    """
    One page of products for a department.

    Returns the decoded API response: ``products`` plus ``totalCount``. The
    request is made with the page's own ``fetch`` so it inherits the WAF token
    and cookies; calling the endpoint from Python would earn a 403.
    """
    payload = {
        "storeContexts": [],
        "filterData": {
            "filter": {
                "showAllDisplayVariants": False,
                "showNotRangedProducts": False,
                "productListSource": {
                    "displayCategory": {
                        "id": department.category_id,
                        "level": department.level,
                        "name": department.name,
                    }
                },
                "paginationOptions": {"page": page_no, "pageSize": page_size},
                "filterOptions": {
                    "filterIds": [],
                    "dealsOnly": False,
                    "brandOptions": [],
                    "departmentOptions": [],
                    "serviceOptions": [],
                    "facetOptions": [],
                },
                "sortOptions": None,
            },
            "displayOptions": {"includeDisplayCategoryTree": True},
        },
        "forYouBonusBuyIds": [],
    }

    result = await page.evaluate(
        """async ({endpoint, payload}) => {
            const res = await fetch(endpoint, {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify(payload),
                credentials: 'include',
            });
            const text = await res.text();
            return {status: res.status, body: text};
        }""",
        {"endpoint": PRODUCTS_ENDPOINT, "payload": payload},
    )

    if result["status"] != 200:
        logger.error(
            "Shoprite API response: status=%s headers=%s body=%r",
            result["status"],
            result.get("headers"),
            result.get("body", "")[:2000],
        )
        raise RuntimeError(
            f"Catalogue API returned {result['status']} for {department.slug} "
            f"page {page_no}."
        )

    try:
        return json.loads(result["body"])
    except json.JSONDecodeError as e:
        raise RuntimeError(f"Catalogue API returned unparseable JSON: {e}") from e


async def iter_department_products(page, department: Department, page_size: int = 50):
    """
    Yield every product in a department, following pagination to the end.

    Stops when a page comes back empty or the running count reaches the
    ``totalCount`` the API reported, whichever happens first. A page that
    returns nothing is the end of the list, not an error.
    """
    page_no = 0
    seen = 0
    while True:
        data = await fetch_products(
            page, department, page_no=page_no, page_size=page_size
        )
        products = data.get("products") or []
        if not products:
            return

        for product in products:
            yield product

        seen += len(products)
        total = data.get("totalCount")
        if total is not None and seen >= total:
            return
        page_no += 1


def product_url(product: Dict[str, Any]) -> str:
    """
    The public page for a product, matching the links the site itself renders:
    /product/<name-slug>-<articleNumber><unitOfMeasure>.

    The card records this so a human can open the product, and so the existing
    maintenance pass keeps finding a Source line it recognises.
    """
    name = product.get("displayName") or product.get("name") or ""
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    article = product.get("articleNumber") or ""
    unit = product.get("unitOfMeasure") or ""
    return f"{BASE_URL}/product/{slug}-{article}{unit}"


def extract_prices(product: Dict[str, Any]) -> Dict[str, Any]:
    """
    Current and previous price in rands, plus whether a promotion is running.

    ``price`` arrives in rands but ``oldPrice`` arrives in minor units scaled by
    ``priceFactor`` (2899 with a factor of 100 is R28.99), so the two cannot be
    compared until the second is converted. A "was" price identical to the
    current one is not a markdown and is dropped.
    """
    price = product.get("price")
    factor = product.get("priceFactor") or 100
    raw_old = product.get("oldPrice")

    old_price = None
    if raw_old:
        old_price = round(raw_old / factor, 2)
        if price is not None and abs(old_price - price) < 0.005:
            old_price = None

    return {
        "current_price": price,
        "was_price": old_price,
        "is_on_promotion": bool(product.get("isOnPromotion")),
    }


def image_urls(product: Dict[str, Any]) -> List[str]:
    """
    Full-size image URLs for a product, without duplicates.

    ``imageIds`` lists the distinct images. ``imageProductCardURL`` and
    ``imagePDPURL`` are not extra pictures: they are those same files resized to
    600px and 300px, so downloading them stores each image three times over and
    leaves the maintenance pass renaming copies. Only when a record carries no
    ``imageIds`` at all is ``imageURL`` used as a fallback.
    """
    urls = [
        f"https://catalog.sixty60.co.za/files/{image_id}"
        for image_id in product.get("imageIds") or []
    ]
    if not urls and product.get("imageURL"):
        urls.append(product["imageURL"])
    return urls


def plain_text(html: Optional[str]) -> str:
    """
    Strip the markup out of an API description.

    Descriptions arrive as HTML fragments (``<p>...</p><p>&nbsp;</p>``). Written
    straight into a card they render as literal tags, so the tags come out, the
    entities are decoded and the empty paragraphs the copy is padded with are
    dropped.
    """
    if not html:
        return ""

    text = re.sub(r"(?i)<br\s*/?>", "\n", html)
    text = re.sub(r"(?i)</p\s*>", "\n\n", text)
    text = re.sub(r"<[^>]+>", "", text)
    text = unescape(text).replace("\xa0", " ")

    paragraphs = [" ".join(p.split()) for p in text.split("\n\n")]
    return "\n\n".join(p for p in paragraphs if p)


async def fetch_product_by_url(page, url: str) -> Optional[Dict[str, Any]]:
    """
    The full product record for one product page, or None if it is not there.

    The product page is server-rendered by Next.js and ships its data in the
    ``__NEXT_DATA__`` script tag as ``pageProps.serverProduct`` - the same shape
    the catalogue API returns. Reading it needs one navigation and no selectors,
    which is what makes it survive the styling changes that killed the old
    scrape.
    """
    response = await page.goto(url, wait_until="domcontentloaded", timeout=60000)
    status = response.status if response else None
    if status != 200:
        logger.warning(f"Product page {url} returned {status}.")
        return None

    raw = await page.evaluate(
        "() => { const e = document.getElementById('__NEXT_DATA__');"
        " return e ? e.textContent : null; }"
    )
    if not raw:
        logger.warning(f"No __NEXT_DATA__ on {url}; the front end may have changed.")
        return None

    try:
        page_props = json.loads(raw)["props"]["pageProps"]
    except (json.JSONDecodeError, KeyError) as e:
        logger.warning(f"Could not read page data for {url}: {e}")
        return None

    return page_props.get("serverProduct")


# Values the brand field carries that are not brands. "Archived" is a catalogue
# status, and the "In-Store" names are the counter a product is made at. They
# sit in the same field as real brands, so they have to be recognised by value.
NON_BRANDS = {"archived", "unknown", "none", "n/a", "no brand", "generic"}


def brand_of(
    product: Dict[str, Any], department: Optional[Department] = None
) -> Optional[str]:
    """
    The product's brand, or None when the field holds something that is not one.

    Roughly half the fresh-food catalogue lists its department where a brand
    belongs - "Bakery" on 44 of 105 sampled bakery lines, "In-Store Deli",
    "Butchery" - and a handful read "Archived". Writing those onto a card states
    something false about the product, so they are dropped rather than recorded.
    Genuine house brands like "The Bakery" and "In-Store Bakery" cannot be told
    apart from the placeholders by value alone; both are the retailer's counters
    either way, so both go.
    """
    brand = (product.get("brand") or "").strip()
    if not brand:
        return None

    lowered = brand.lower()
    if lowered in NON_BRANDS or lowered.startswith("in-store"):
        return None

    # A brand that only repeats the aisle it was found in is the aisle, not a
    # brand: "Bakery" under /department/bakery, "Butchery" under butchery.
    if department is not None:
        aisle = {department.section.replace("-", " ").lower(), department.name.lower()}
        bare = lowered[4:] if lowered.startswith("the ") else lowered
        if lowered in aisle or bare in aisle:
            return None

    return brand
