import os
import json
import re
import datetime
import hashlib
from typing import List, Dict, Any


def parse_nutrition_table(content: str) -> List[Dict[str, str]]:
    nutrition = []

    # Find the table in ## Nutrition Information section
    section_match = re.search(
        r'## Nutrition Information\n(.*?)(?:\n##|\Z)',
        content,
        re.DOTALL
    )

    if not section_match:
        return []

    table_content = section_match.group(1).strip()
    lines = table_content.split('\n')

    # Need at least header, separator, and one row
    if len(lines) < 3:
        return []

    # Simple markdown table parser
    for line in lines:
        if '|' in line and '---' not in line:
            cells = [c.strip() for c in line.split('|')]

            # Skip header row
            if "Nutrient" in cells:
                continue

            if len(cells) >= 4:
                nutrient = cells[1]
                per_100 = cells[2]
                per_serving = cells[3] if len(cells) > 3 else ""

                if nutrient and (per_100 or per_serving):
                    nutrition.append({
                        "nutrient": nutrient,
                        "per_100g": per_100,
                        "per_serving": per_serving
                    })

    return nutrition


def extract_category_info(source_url: str) -> Dict[str, Any]:
    """
    Example:
    https://www.shoprite.co.za/All-Departments/Food/Bakery/Bread-and-Rolls/Blue-Ribbon-Sliced-White-Bread-700g/p/10136370EA
    """

    match = re.search(
        r'shoprite\.co\.za/(.*?)/[^/]+/p/(\d+)',
        source_url
    )

    if not match:
        return {
            "category_path": [],
            "category": "Uncategorized",
            "product_id": None
        }

    path_str = match.group(1)
    product_id = match.group(2)

    category_path = path_str.split('/')
    category = category_path[-1] if category_path else "Uncategorized"

    return {
        "category_path": category_path,
        "category": category,
        "product_id": product_id
    }


def get_smallest_image(images: List[str]) -> str | None:
    """
    Return the smallest image based on WxH filename pattern.
    Example:
    slug_300x300.jpg
    slug_1200x1200.jpg
    """

    def image_area(path: str) -> float:
        match = re.search(r'_(\d+)x(\d+)', path)

        if not match:
            return float("inf")

        width = int(match.group(1))
        height = int(match.group(2))

        return width * height

    return sorted(images, key=image_area)[0] if images else None


def publish():
    products_root = "products"
    published_dir = "published"

    os.makedirs(published_dir, exist_ok=True)

    all_products = []

    for root, dirs, files in os.walk(products_root):
        for file in files:
            if not file.endswith("_card.md"):
                continue

            card_path = os.path.join(root, file)
            slug = file.replace("_card.md", "")

            with open(card_path, 'r', encoding='utf-8') as f:
                content = f.read()

            # -----------------------------------------------------------------
            # Basic fields
            # -----------------------------------------------------------------

            name_match = re.search(r'^# (.*)', content)
            name = name_match.group(1).strip() if name_match else slug

            current_price_match = re.search(
                r'- \*\*Current Price\*\*: R([\d\.]+)',
                content
            )

            current_price = (
                current_price_match.group(1)
                if current_price_match else None
            )

            was_price_match = re.search(
                r'- \*\*Was\*\*: R([\d\.]+)',
                content
            )

            was_price = (
                was_price_match.group(1)
                if was_price_match else None
            )

            description_match = re.search(
                r'## Description\n(.*?)(?:\n##|\Z)',
                content,
                re.DOTALL
            )

            description = (
                description_match.group(1).strip()
                if description_match else ""
            )

            # -----------------------------------------------------------------
            # Images
            # -----------------------------------------------------------------

            images_match = re.search(
                r'## Images\n(.*?)(?:\n##|\Z)',
                content,
                re.DOTALL
            )

            images = []

            if images_match:
                raw_images = [
                    line.strip().replace('- ', '')
                    for line in images_match.group(1).strip().split('\n')
                    if line.strip().startswith('- ')
                ]

                images = [
                    f"products/{slug}/{img}"
                    for img in raw_images
                ]

            thumbnail = get_smallest_image(images)

            # -----------------------------------------------------------------
            # Source + scraped
            # -----------------------------------------------------------------

            source_match = re.search(
                r'- \*\*Source\*\*: (.*)',
                content
            )

            source_url = (
                source_match.group(1).strip()
                if source_match else ""
            )

            scraped_match = re.search(
                r'- \*\*Scraped\*\*: (.*)',
                content
            )

            scraped_date = (
                scraped_match.group(1).strip()
                if scraped_match else ""
            )

            cat_info = extract_category_info(source_url)

            # -----------------------------------------------------------------
            # Nutrition
            # -----------------------------------------------------------------

            nutrition = parse_nutrition_table(content)

            # -----------------------------------------------------------------
            # Generic section parser
            # -----------------------------------------------------------------

            def get_section_content(section_name: str) -> str:
                match = re.search(
                    fr'## {section_name}\n(.*?)(?:\n##|\Z)',
                    content,
                    re.DOTALL
                )

                return match.group(1).strip() if match else ""

            ingredients = get_section_content("Ingredients")
            allergens = get_section_content("Allergens")
            benefits = get_section_content("Benefits & Features")

            # -----------------------------------------------------------------
            # Specifications
            # -----------------------------------------------------------------

            specifications = {}

            specs_content = get_section_content("Specifications")

            if specs_content:
                for line in specs_content.split('\n'):
                    line = line.strip()

                    if line.startswith('- **'):
                        m = re.search(
                            r'- \*\*(.*?)\*\*:\s*(.*)',
                            line
                        )

                        if m:
                            specifications[m.group(1)] = m.group(2).strip()

            barcode = specifications.get("Main Barcode")
            brand = specifications.get("Product Brand")
            weight = specifications.get("Product Weight")

            # -----------------------------------------------------------------
            # Meta
            # -----------------------------------------------------------------

            is_platform = False

            meta_match = re.search(
                r'## Meta\n(.*?)(?:\n##|\Z)',
                content,
                re.DOTALL
            )

            if meta_match:
                if "- **Is Platform**: true" in meta_match.group(1):
                    is_platform = True

            # -----------------------------------------------------------------
            # Product JSON
            # -----------------------------------------------------------------

            product_data = {
                "id": slug,
                "product_id": cat_info["product_id"],
                "name": name,
                "category": cat_info["category"],
                "category_path": cat_info["category_path"],
                "current_price": current_price,
                "was_price": was_price,
                "description": description,
                "ingredients": ingredients,
                "allergens": allergens,
                "benefits": benefits,
                "barcode": barcode,
                "brand": brand,
                "weight": weight,
                "specifications": specifications,
                "is_platform": is_platform,
                "nutrition": nutrition,
                "thumbnail": thumbnail,
                "images": images,
                "source_url": source_url,
                "scraped_date": scraped_date
            }

            # Save full product JSON
            product_json_path = os.path.join(root, f"{slug}.json")

            with open(product_json_path, 'w', encoding='utf-8') as f:
                json.dump(product_data, f, indent=2)

            # -----------------------------------------------------------------
            # Index entry
            # -----------------------------------------------------------------

            all_products.append({
                "id": slug,
                "product_id": cat_info["product_id"],
                "name": name,
                "category": cat_info["category"],
                "category_path": cat_info["category_path"],
                "current_price": current_price,
                "was_price": was_price,
                "barcode": barcode,
                "brand": brand,
                "is_platform": is_platform,
                "thumbnail": thumbnail,
                "image_count": len(images),
                "scraped_date": scraped_date
            })

    # -------------------------------------------------------------------------
    # Build category tree
    # -------------------------------------------------------------------------

    category_tree = {
        "name": "root",
        "count": 0,
        "children": {}
    }

    for p in all_products:
        current = category_tree
        current["count"] += 1

        for segment in p["category_path"]:
            if segment not in current["children"]:
                current["children"][segment] = {
                    "name": segment,
                    "count": 0,
                    "children": {}
                }

            current = current["children"][segment]
            current["count"] += 1

    def format_tree(node):
        return {
            "name": node["name"],
            "count": node["count"],
            "children": [
                format_tree(child)
                for child in node["children"].values()
            ]
        }

    formatted_tree = format_tree(category_tree)["children"]

    # -------------------------------------------------------------------------
    # Content hash
    # -------------------------------------------------------------------------

    all_products.sort(key=lambda x: x["id"])

    hash_input = "".join([
        f"{p['id']}"
        f"{p['scraped_date']}"
        f"{p['current_price']}"
        f"{p['image_count']}"
        for p in all_products
    ])

    content_hash = hashlib.md5(
        hash_input.encode('utf-8')
    ).hexdigest()

    # Remove temporary fields
    for p in all_products:
        p.pop("scraped_date", None)
        p.pop("image_count", None)

    # -------------------------------------------------------------------------
    # Meta JSON
    # -------------------------------------------------------------------------

    meta = {
        "total": len(all_products),
        "product_count": len(all_products),
        "content_hash": content_hash,
        "last_updated": datetime.datetime.now().isoformat(),
        "category_tree": formatted_tree,
        "products": all_products
    }

    with open(
        os.path.join(published_dir, "meta.json"),
        'w',
        encoding='utf-8'
    ) as f:
        json.dump(meta, f, indent=2)

    # -------------------------------------------------------------------------
    # Platform products
    # -------------------------------------------------------------------------

    platform_products = [
        p for p in all_products
        if p.get("is_platform")
    ]

    with open(
        os.path.join(published_dir, "platform_products.json"),
        'w',
        encoding='utf-8'
    ) as f:
        json.dump(platform_products, f, indent=2)

    # -------------------------------------------------------------------------
    # Flat categories
    # -------------------------------------------------------------------------

    categories_map = {}

    def traverse_categories(nodes):
        for node in nodes:
            name = node["name"]

            categories_map[name] = (
                categories_map.get(name, 0)
                + node["count"]
            )

            traverse_categories(node.get("children", []))

    traverse_categories(formatted_tree)

    flat_categories = [
        {
            "name": k,
            "count": v
        }
        for k, v in categories_map.items()
    ]

    flat_categories.sort(key=lambda x: x["name"])

    with open(
        os.path.join(published_dir, "categories.json"),
        'w',
        encoding='utf-8'
    ) as f:
        json.dump(flat_categories, f, indent=2)


if __name__ == "__main__":
    publish()