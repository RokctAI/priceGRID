# PriceGrid Product Scraper

This directory contains scripts to scrape product information from the PriceGrid website (shoprite.co.za).

## Scripts

### 1. `catalogue_api.py`

The catalogue layer. Both storefronts run the same front end, which serves its
data as JSON from `POST /api/catalogue/get-products-filter` rather than as
markup. This module discovers departments, pages through their products
and reads a single product's record off its page.

A WAF fronts both sites and issues a JavaScript challenge, so plain HTTP
clients get 403 where a browser gets 200. Every call here is therefore made from
inside a Playwright page, which already holds the WAF token.

### 2. `scraper.py`

The main scraper. It walks every department and writes a markdown card per
product.

**Usage:**

```bash
python3 scripts/scrappers/shoprite/scraper.py [args]
```

**Arguments:**

- `--store`: `shoprite` (default) or `checkers`.
- `--category`: only scrape departments whose path contains this text, e.g.
  `bakery`. Defaults to all of them.
- `--limit`: stop after this many new cards. Default is 0 (no limit).
- `--page-size`: products per API request. Default is 50.
- `--headless`: run the browser headless. CI sets this.
- `--legacy-html`: use the pre-2026 HTML scrape instead. Kept for reference;
  the pages it reads no longer exist.

**Functionality:**

- Extracts: name, current and previous price, promotion state, description,
  brand, barcode and images.
- Deduplicates by product id within a run, and skips products that already have
  a card.
- Exits non-zero when it cannot read the catalogue, so a blocked run fails
  rather than quietly committing nothing.
- Logs to `.rokct/agent/logs/shoprite_scraper.log` and
  `.rokct/agent/logs/shoprite_failures.log`.

### 3. `maintain.py`

Refreshes prices on cards that already exist, then tidies the images.

**Usage:**

```bash
python3 scripts/scrappers/shoprite/maintain.py [--images-only]
```

**Functionality:**

- Iterates every `_card.md` under `products/` and re-reads its Source URL.
- Updates the Price section when it has changed.
- Renames images after their real dimensions; a product whose dimensions cannot
  be read is left alone rather than renamed to `_0x0`.
- Exits non-zero when every fetch failed, which is what being blocked looks
  like.

## Output Structure

The scraped data is stored in the `products/` directory:

```text
products/
└── {product-slug}/
    ├── images/
    │   ├── {product-slug}_0.jpg
    │   ├── {product-slug}_1.jpg
    │   └── ...
    └── {product-slug}_card.md
```

### Card Format (`_card.md`)

```markdown
# {Product Name}

## Price
- **Current Price**: R{price}
- **Was**: R{was_price}  ← omitted if not on promotion

## Description
{description}

## Nutrition Information

| Nutrient | Per 100g | Per Serving |
|----------|----------|-------------|
| ...      | ...      | ...         |

## Images
- images/{filename_1}
- images/{filename_2}

## Meta
- **Source**: {product_url}
- **Scraped**: {date}
- **Store**: Shoprite/Checkers
```
