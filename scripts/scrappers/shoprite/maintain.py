import random
import os
import sys
import asyncio
import logging
import re
import argparse
from typing import Optional, Dict
from playwright.async_api import async_playwright

sys.path.append(os.path.dirname(__file__))
from scraper import (
    extract_price_from_page,
    JS_PRICE_EXTRACTION,
    get_hardened_context,
    get_stealthy_page,
)

# Setup logging
os.makedirs(".rokct/agent/logs", exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(".rokct/agent/logs/shoprite_maintain.log", mode="w"),
        logging.StreamHandler(sys.stdout),
    ],
)
logger = logging.getLogger(__name__)


def get_image_dimensions(filepath: str):
    """Return (width, height) for an image file, or (0, 0) on failure."""
    try:
        from PIL import Image

        with Image.open(filepath) as img:
            return img.size  # (width, height)
    except Exception:
        return (0, 0)


def rename_images_by_size(product_dir: str, card_path: str, slug: str) -> bool:
    """
    Rename every image in product_dir/images/ to {slug}_{W}x{H}.jpg.
    If two images share the same dimensions, append _1, _2, etc.
    Updates the card file to reflect the new names.
    Returns True if the card was modified.
    """
    images_dir = os.path.join(product_dir, "images")
    if not os.path.exists(images_dir):
        return False

    image_files = sorted(
        [
            f
            for f in os.listdir(images_dir)
            if f.lower().endswith((".jpg", ".jpeg", ".png", ".webp"))
        ]
    )
    if not image_files:
        return False

    # Build rename map: old_name -> new_name
    seen_dims = {}  # dim_key -> count
    rename_map = {}

    for fname in image_files:
        fpath = os.path.join(images_dir, fname)
        w, h = get_image_dimensions(fpath)
        dim_key = f"{w}x{h}"
        count = seen_dims.get(dim_key, 0)
        seen_dims[dim_key] = count + 1

        if count == 0:
            new_name = f"{slug}_{dim_key}.jpg"
        else:
            new_name = f"{slug}_{dim_key}_{count}.jpg"

        rename_map[fname] = new_name

    # Check if anything actually needs renaming
    if all(old == new for old, new in rename_map.items()):
        return False

    # Perform renames (use a temp name first to avoid collisions)
    for old_name, new_name in rename_map.items():
        old_path = os.path.join(images_dir, old_name)
        new_path = os.path.join(images_dir, new_name)
        if old_path == new_path:
            continue
        # Two-step to avoid clobbering if new_name already exists with different content
        tmp_path = old_path + ".tmp_rename"
        os.rename(old_path, tmp_path)
        rename_map[old_name] = (tmp_path, new_path)

    # Second pass: move from .tmp_rename to final name
    for old_name, val in rename_map.items():
        if isinstance(val, tuple):
            tmp_path, new_path = val
            if os.path.exists(tmp_path):
                os.rename(tmp_path, new_path)
                logger.info(
                    f"Renamed image: {old_name} -> {os.path.basename(new_path)}"
                )

    # Rewrite the ## Images section in the card
    with open(card_path, "r", encoding="utf-8") as f:
        content = f.read()

    # Build old->new for card references (card stores "images/filename")
    card_rename = {
        f"images/{old}": f"images/{(val[1] if isinstance(val, tuple) else val)}"
        for old, val in rename_map.items()
    }

    new_content = content
    for old_ref, new_ref in card_rename.items():
        new_content = new_content.replace(
            old_ref,
            os.path.basename(new_ref)
            .join(["images/", ""])
            .replace("images/images/", "images/"),
        )

    # Simpler: just do a direct replace of old filename -> new filename in the Images section
    new_content = content
    for old, val in rename_map.items():
        new_name = os.path.basename(val[1]) if isinstance(val, tuple) else val
        new_content = new_content.replace(f"images/{old}", f"images/{new_name}")

    if new_content != content:
        with open(card_path, "w", encoding="utf-8") as f:
            f.write(new_content)
        return True

    return False


def maintain_images():
    products_root = "products"
    if not os.path.exists(products_root):
        logger.info("No products folder found for image maintenance.")
        return

    logger.info("Starting image maintenance...")
    for root, dirs, files in os.walk(products_root):
        for file in files:
            if file.endswith("_card.md"):
                card_path = os.path.join(root, file)
                product_dir = root
                slug = file.replace("_card.md", "")

                with open(card_path, "r", encoding="utf-8") as f:
                    content = f.read()

                # ── Rename images by size ─────────────────────────────────────
                renamed = rename_images_by_size(product_dir, card_path, slug)
                if renamed:
                    logger.info(f"Renamed images for {slug}")
                    # Reload content after rename update
                    with open(card_path, "r", encoding="utf-8") as f:
                        content = f.read()

                # ── Sync: delete files not listed in card ─────────────────────
                image_section = re.search(r"## Images\n((?:- images/.*\n?)*)", content)
                listed_images = []
                if image_section:
                    listed_images = [
                        line.strip().replace("- ", "")
                        for line in image_section.group(1).strip().split("\n")
                        if line.strip().startswith("- images/")
                    ]

                images_dir = os.path.join(product_dir, "images")
                if not os.path.exists(images_dir):
                    if listed_images:
                        logger.warning(
                            f"Images dir missing but images listed in {card_path}"
                        )
                    continue

                for actual_file in os.listdir(images_dir):
                    relative = f"images/{actual_file}"
                    if relative not in listed_images:
                        os.remove(os.path.join(images_dir, actual_file))
                        logger.info(f"Deleted unlisted image: {relative}")

                for listed_image in listed_images:
                    if not os.path.exists(os.path.join(product_dir, listed_image)):
                        logger.warning(
                            f"Listed image missing on disk: {listed_image} (in {card_path})"
                        )

    # ── Ensure Is Platform field exists in all cards ──────────────────────────
    logger.info("Ensuring 'Is Platform' field exists in all cards...")
    for root, dirs, files in os.walk(products_root):
        for file in files:
            if file.endswith("_card.md"):
                card_path = os.path.join(root, file)
                with open(card_path, "r", encoding="utf-8") as f:
                    content = f.read()

                if "- **Is Platform**:" not in content:
                    if "## Meta" in content:
                        new_content = content.replace(
                            "## Meta", "## Meta\n- **Is Platform**: false"
                        )
                    else:
                        new_content = (
                            content.rstrip() + "\n\n## Meta\n- **Is Platform**: false\n"
                        )

                    with open(card_path, "w", encoding="utf-8") as f:
                        f.write(new_content)
                    logger.info(f"Added 'Is Platform: false' to {card_path}")

    logger.info("Image maintenance complete.")


async def update_price(context, card_path: str):
    with open(card_path, "r") as f:
        content = f.read()

    match = re.search(r"- \*\*Source\*\*: (https://www\.shoprite\.co\.za/.*)", content)
    if not match:
        logger.warning(f"Could not find source URL in {card_path}")
        return

    url = match.group(1).strip()
    logger.info(f"Updating price for: {url}")

    page = None
    try:
        page = await get_stealthy_page(context)
        try:
            response = await page.goto(
                url, wait_until="domcontentloaded", timeout=60000
            )
            status = response.status if response else "No Response"

            if not response or status != 200:
                logger.error(
                    f"Failed to load {url} (Status: {status}). Skipping product."
                )
                return
        except Exception as e:
            logger.error(f"Exception loading {url}: {e}")
            return

        await page.evaluate("window.scrollTo(0, document.body.scrollHeight / 2)")
        await page.wait_for_timeout(3000)

        data = await page.evaluate(JS_PRICE_EXTRACTION)

        prices = extract_price_from_page(data)
        current_price = prices["current_price"]
        was_price = prices["was_price"]

        if not current_price:
            logger.warning(f"Could not extract current price for {url}")
            return

        price_section = f"## Price\n- **Current Price**: R{current_price}"
        if prices.get("is_card_price"):
            price_section += " (WITH CARD)"
        if was_price:
            price_section += f"\n- **Was**: R{was_price}"
        if prices.get("promotion_dates"):
            price_section += f"\n- **Validity**: {prices.get('promotion_dates')}"

        new_content = re.sub(
            r"## Price\n(?:- .*\n?)*\n(?=## Description)",
            price_section + "\n\n",
            content,
        )

        if new_content != content:
            with open(card_path, "w") as f:
                f.write(new_content)
            logger.info(f"Successfully updated price in {card_path}")
        else:
            logger.info(f"Price unchanged for {card_path}")

    except Exception as e:
        logger.error(f"Error updating price for {url}: {e}")
    finally:
        if page:
            await page.close()


async def main():
    parser = argparse.ArgumentParser(description="Maintain PriceGrid product data.")
    parser.add_argument(
        "--images-only",
        action="store_true",
        help="Only perform image maintenance, skip price updates.",
    )
    args = parser.parse_args()

    if not args.images_only:
        async with async_playwright() as p:
            browser = await p.chromium.launch(
                headless=True,
                args=[
                    "--disable-blink-features=AutomationControlled",
                    "--no-sandbox",
                    "--disable-dev-shm-usage",
                ],
            )
            context = await get_hardened_context(browser)

            products_root = "products"
            if not os.path.exists(products_root):
                logger.info("No products folder found.")
                await browser.close()
                return

            cards = []
            for root, dirs, files in os.walk(products_root):
                for file in files:
                    if file.endswith("_card.md"):
                        cards.append(os.path.join(root, file))

            logger.info("Establishing cookies via home page...")
            temp_page = await get_stealthy_page(context)
            try:
                await temp_page.goto(
                    "https://www.shoprite.co.za",
                    wait_until="domcontentloaded",
                    timeout=30000,
                )
                await asyncio.sleep(random.uniform(2, 4))
            except Exception as e:
                logger.warning(f"Failed to load home page: {e}")
            finally:
                await temp_page.close()

            logger.info(f"Found {len(cards)} product cards to update.")
            for card_path in cards:
                await update_price(context, card_path)
                await asyncio.sleep(random.uniform(3, 7))

            await browser.close()

    maintain_images()


if __name__ == "__main__":
    asyncio.run(main())
