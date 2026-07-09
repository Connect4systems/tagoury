from __future__ import annotations

import html
import json
import re
from typing import Any

import frappe

from tagoury.import_tagoury_catalog import (
	PROMOTIONAL_GROUP_SLUGS,
	ROOT_ITEM_GROUP,
	build_item_description,
	category_depth,
	choose_item_group,
	clean_html,
	clean_text,
	fetch_catalog,
	to_number,
	unique_group_name,
)


MAIN_NAV = [
	("New Collection", "/shop?item_group=New%20Collection"),
	("VIP Projects", "/vip-projects"),
	("Limited Editions", "/shop?item_group=Limited%20Editions"),
	("Best Selling", "/shop?item_group=Best%20Selling"),
	("Indoor", "/shop?item_group=Indoor"),
	("Outdoor", "/shop?item_group=Outdoor"),
	("Our Services", "#"),
]

MENU_CATEGORY_ORDER = {
	"Indoor": [
		"Accessories",
		"Boxes",
		"Candles and Lanterns",
		"Chairs",
		"Chest of drawers",
		"Consoles",
		"Lighting",
		"Natural Wood",
		"Ottomans and stools",
		"Paravan",
		"Pillows and Textiles",
		"Rugs",
		"Shelves",
		"Sofas",
		"Tables",
		"Trolleys",
		"Beds",
		"Fans",
		"Unique Desk",
		"TV Units",
		"Bars",
		"Parquet",
	],
	"Outdoor": [
		"Accessories",
		"Candles and Lanterns",
		"Chairs",
		"Lighting",
		"Natural Wood",
		"Ottomans and stools",
		"Pillows and Textiles",
		"Sets",
		"Sun Beds",
		"Tables",
		"Fans",
		"Shading Solutions",
		"Sofas",
		"Rugs",
		"Parquet",
	],
}

SERVICE_NAV = [
	("Feng Shui Service", "/services#feng-shui"),
	("Boutique Hotel Service", "/services#boutique-hotel"),
	("Hotel Furniture Services", "/services#hotel-furniture"),
	("Linens Catalog", "/services#linens-catalog"),
]

FOOTER_LINKS = [
	("About Us", "/about-us"),
	("Our Story", "/our-story"),
	("Return & Exchange Policy", "/return-exchange-policy"),
	("VIP Projects", "/vip-projects"),
]

PHONE_NUMBERS = ["01225255553", "01222107486"]


def publish_all(limit: int | None = None) -> dict[str, Any]:
	"""Publish imported Tagoury's House items to Webshop and create storefront pages."""
	ensure_webshop_installed()
	catalog = fetch_catalog(limit=limit)
	categories = {category["id"]: category for category in catalog["categories"]}
	group_names = {category["id"]: unique_group_name(category, categories) for category in catalog["categories"]}

	report: dict[str, Any] = {
		"products_fetched": len(catalog["products"]),
		"website_items_created": 0,
		"website_items_updated": 0,
		"item_prices_updated": 0,
		"custom_fields_created": 0,
		"pages_created": 0,
		"pages_updated": 0,
		"settings_updated": 0,
		"skipped": [],
	}

	report["custom_fields_created"] = ensure_custom_fields()
	ensure_item_groups_visible(catalog["categories"], group_names)
	ensure_item_prices(catalog["products"], report)

	home_products = []
	for product in catalog["products"]:
		sku = clean_text(product.get("sku") or "") or f"TH-{product['id']}"
		if not frappe.db.exists("Item", sku):
			report["skipped"].append({"item": sku, "reason": "Item does not exist"})
			continue

		item_group = choose_item_group(product, categories, group_names)
		price_info = product_price_info(product)
		update_item_website_fields(sku, product, item_group, price_info)
		sync_website_item(sku, product, item_group, categories, group_names, report)
		home_products.append(home_product_payload(product, sku, item_group, price_info))

	create_storefront_pages(catalog, home_products, report)
	frappe.db.commit()
	return report


def ensure_webshop_installed() -> None:
	if not frappe.db.exists("DocType", "Website Item"):
		frappe.throw("Webshop is not installed. Install payments and webshop version-15 first.")


def ensure_custom_fields() -> int:
	fields = [
		{
			"dt": "Item",
			"fieldname": "tagoury_regular_price",
			"label": "Tagoury Regular Price",
			"fieldtype": "Currency",
			"insert_after": "standard_rate",
		},
		{
			"dt": "Item",
			"fieldname": "tagoury_sale_price",
			"label": "Tagoury Sale Price",
			"fieldtype": "Currency",
			"insert_after": "tagoury_regular_price",
		},
		{
			"dt": "Item",
			"fieldname": "tagoury_on_sale",
			"label": "Tagoury On Sale",
			"fieldtype": "Check",
			"insert_after": "tagoury_sale_price",
		},
		{
			"dt": "Item",
			"fieldname": "tagoury_source_url",
			"label": "Tagoury Source URL",
			"fieldtype": "Small Text",
			"insert_after": "tagoury_on_sale",
		},
		{
			"dt": "Website Item",
			"fieldname": "tagoury_regular_price",
			"label": "Tagoury Regular Price",
			"fieldtype": "Currency",
			"insert_after": "item_code",
		},
		{
			"dt": "Website Item",
			"fieldname": "tagoury_sale_price",
			"label": "Tagoury Sale Price",
			"fieldtype": "Currency",
			"insert_after": "tagoury_regular_price",
		},
		{
			"dt": "Website Item",
			"fieldname": "tagoury_on_sale",
			"label": "Tagoury On Sale",
			"fieldtype": "Check",
			"insert_after": "tagoury_sale_price",
		},
	]
	created = 0
	for field in fields:
		name = f"{field['dt']}-{field['fieldname']}"
		if frappe.db.exists("Custom Field", name):
			doc = frappe.get_doc("Custom Field", name)
			needs_save = False
			for key, value in field.items():
				if doc.get(key) != value:
					doc.set(key, value)
					needs_save = True
			if needs_save:
				doc.save(ignore_permissions=True)
			continue
		frappe.get_doc({"doctype": "Custom Field", **field}).insert(ignore_permissions=True)
		created += 1
	frappe.clear_cache(doctype="Item")
	frappe.clear_cache(doctype="Website Item")
	return created


def ensure_item_groups_visible(
	categories: list[dict[str, Any]],
	group_names: dict[int, str],
) -> None:
	fields = meta_fields("Item Group")
	parent_ids = {int(category.get("parent") or 0) for category in categories}
	for category_id, group_name in group_names.items():
		if not frappe.db.exists("Item Group", group_name):
			continue
		doc = frappe.get_doc("Item Group", group_name)
		if "show_in_website" in fields:
			doc.set("show_in_website", 1)
		if "is_group" in fields:
			doc.set("is_group", 1 if category_id in parent_ids else 0)
		doc.save(ignore_permissions=True)


def ensure_item_prices(products: list[dict[str, Any]], report: dict[str, Any]) -> None:
	ensure_price_list("Standard Selling", "EGP")
	for product in products:
		sku = clean_text(product.get("sku") or "") or f"TH-{product['id']}"
		if not frappe.db.exists("Item", sku):
			continue
		price_info = product_price_info(product)
		if price_info["sale_price"] is not None:
			upsert_item_price(sku, "Standard Selling", price_info["sale_price"], price_info["currency"])
			report["item_prices_updated"] += 1


def ensure_price_list(name: str, currency: str) -> None:
	if frappe.db.exists("Price List", name):
		return
	frappe.get_doc(
		{
			"doctype": "Price List",
			"price_list_name": name,
			"enabled": 1,
			"selling": 1,
			"currency": currency,
		}
	).insert(ignore_permissions=True)


def upsert_item_price(item_code: str, price_list: str, rate: float, currency: str) -> None:
	name = frappe.db.get_value(
		"Item Price",
		{"item_code": item_code, "price_list": price_list, "selling": 1},
		"name",
	)
	values = {
		"doctype": "Item Price",
		"item_code": item_code,
		"price_list": price_list,
		"selling": 1,
		"currency": currency,
		"price_list_rate": rate,
	}
	if name:
		doc = frappe.get_doc("Item Price", name)
		doc.update(values)
		doc.save(ignore_permissions=True)
	else:
		frappe.get_doc(values).insert(ignore_permissions=True)


def update_item_website_fields(
	item_code: str,
	product: dict[str, Any],
	item_group: str,
	price_info: dict[str, Any],
) -> None:
	fields = meta_fields("Item")
	item = frappe.get_doc("Item", item_code)
	values = {
		"description": build_item_description(product),
		"item_group": item_group,
		"standard_rate": price_info["sale_price"],
		"tagoury_regular_price": price_info["regular_price"],
		"tagoury_sale_price": price_info["sale_price"],
		"tagoury_on_sale": 1 if price_info["on_sale"] else 0,
		"tagoury_source_url": product.get("permalink"),
	}
	for field in ["show_in_website", "published_in_website"]:
		values[field] = 1
	values["route"] = product_route(product)

	for field, value in values.items():
		if field in fields:
			item.set(field, fit_field_value("Item", field, value))
	item.save(ignore_permissions=True)


def sync_website_item(
	item_code: str,
	product: dict[str, Any],
	item_group: str,
	categories: dict[int, dict[str, Any]],
	group_names: dict[int, str],
	report: dict[str, Any],
) -> None:
	fields = meta_fields("Website Item")
	name = frappe.db.get_value("Website Item", {"item_code": item_code}, "name")
	item = frappe.get_doc("Item", item_code)
	description = build_item_description(product)
	values = {
		"doctype": "Website Item",
		"item_code": item_code,
		"web_item_name": clean_text(product["name"]),
		"item_name": clean_text(product["name"]),
		"item_group": item_group,
		"route": product_route(product),
		"published": 1,
		"description": description,
		"short_description": clean_html(product.get("short_description") or ""),
		"website_image": item.image,
		"thumbnail": item.image,
		"tagoury_regular_price": product_price_info(product)["regular_price"],
		"tagoury_sale_price": product_price_info(product)["sale_price"],
		"tagoury_on_sale": 1 if product_price_info(product)["on_sale"] else 0,
	}
	filtered_values = {field: value for field, value in values.items() if field == "doctype" or field in fields}
	if name:
		doc = frappe.get_doc("Website Item", name)
		doc.update(filtered_values)
		set_website_item_groups(doc, product, categories, group_names)
		set_website_item_offers(doc, product_price_info(product))
		doc.save(ignore_permissions=True)
		report["website_items_updated"] += 1
	else:
		doc = frappe.get_doc(filtered_values)
		set_website_item_groups(doc, product, categories, group_names)
		set_website_item_offers(doc, product_price_info(product))
		doc.insert(ignore_permissions=True)
		report["website_items_created"] += 1


def set_website_item_groups(
	doc: Any,
	product: dict[str, Any],
	categories: dict[int, dict[str, Any]],
	group_names: dict[int, str],
) -> None:
	table_field = get_table_field("Website Item", ["website_item_groups"], "Website Item Groups")
	if not table_field or not table_field.options:
		return
	child_fields = meta_fields(table_field.options)
	if "item_group" not in child_fields:
		return

	doc.set(table_field.fieldname, [])
	for category in product.get("categories") or []:
		group_name = group_names.get(category.get("id"))
		if group_name and frappe.db.exists("Item Group", group_name):
			doc.append(table_field.fieldname, {"item_group": group_name})


def set_website_item_offers(doc: Any, price_info: dict[str, Any]) -> None:
	table_field = get_table_field("Website Item", ["offers"], "Offers")
	if not table_field or not table_field.options:
		return
	doc.set(table_field.fieldname, [])
	if not price_info["on_sale"]:
		return

	child_fields = meta_fields(table_field.options)
	filtered_row = {}
	set_child_value(
		filtered_row,
		table_field.options,
		["offer_title", "title"],
		"Offer Title",
		f"Was {format_price(price_info['regular_price'], price_info['currency'])}",
	)
	set_child_value(
		filtered_row,
		table_field.options,
		["offer_subtitle", "subtitle"],
		"Offer Subtitle",
		f"Now {format_price(price_info['sale_price'], price_info['currency'])}",
	)
	filtered_row = {field: value for field, value in filtered_row.items() if field in child_fields}
	if filtered_row:
		doc.append(table_field.fieldname, filtered_row)


def create_storefront_pages(
	catalog: dict[str, Any],
	home_products: list[dict[str, Any]],
	report: dict[str, Any],
) -> None:
	categories = catalog["categories"]
	products_by_section = {
		"New Collection": products_for_category(home_products, "new-collection")[:8],
		"Best Selling": products_for_category(home_products, "best-selling")[:8],
		"Offers": [item for item in home_products if item["on_sale"]][:8],
	}
	top_categories = [
		category for category in categories if not int(category.get("parent") or 0) and category.get("count", 0)
	]
	home_html = build_homepage_html(products_by_section, top_categories)
	upsert_web_page("home", "Tagoury's House", home_html, report)
	upsert_web_page("about-us", "About Us", about_html(), report)
	upsert_web_page("our-story", "Our Story", story_html(), report)
	upsert_web_page("return-exchange-policy", "Return & Exchange Policy", policy_html(), report)
	upsert_web_page("vip-projects", "VIP Projects", vip_projects_html(), report)
	upsert_web_page("services", "Our Services", services_html(), report)
	update_website_settings(catalog, report)


def upsert_web_page(route: str, title: str, content: str, report: dict[str, Any]) -> None:
	name = frappe.db.get_value("Web Page", {"route": route}, "name")
	fields = meta_fields("Web Page")
	values = {
		"doctype": "Web Page",
		"title": title,
		"route": route,
		"published": 1,
		"content_type": "HTML",
		"main_section": content,
	}
	values = {field: value for field, value in values.items() if field == "doctype" or field in fields}
	if name:
		doc = frappe.get_doc("Web Page", name)
		doc.update(values)
		doc.save(ignore_permissions=True)
		report["pages_updated"] += 1
	else:
		frappe.get_doc(values).insert(ignore_permissions=True)
		report["pages_created"] += 1


def update_website_settings(catalog: dict[str, Any], report: dict[str, Any]) -> None:
	settings = frappe.get_single("Website Settings")
	fields = meta_fields("Website Settings")
	if "home_page" in fields:
		settings.home_page = "home"
	if "top_bar_items" in fields and hasattr(settings, "append"):
		settings.top_bar_items = []
		child_field = settings.meta.get_field("top_bar_items")
		child_fields = meta_fields(child_field.options) if child_field and child_field.options else set()
		for row in navbar_rows(catalog):
			filtered_row = {field: value for field, value in row.items() if field in child_fields}
			if filtered_row:
				settings.append("top_bar_items", filtered_row)
	settings.save(ignore_permissions=True)
	report["settings_updated"] += 1


def navbar_rows(catalog: dict[str, Any]) -> list[dict[str, Any]]:
	"""Build navbar rows, including one-level Item Group dropdown entries."""
	categories = {int(category["id"]): category for category in catalog["categories"]}
	group_names = {
		category_id: unique_group_name(category, categories)
		for category_id, category in categories.items()
	}
	category_ids_by_name = {name: category_id for category_id, name in group_names.items()}
	rows: list[dict[str, Any]] = []

	for label, url in MAIN_NAV:
		rows.append({"label": label, "url": url, "right": 0})
		if label == "Our Services":
			rows.extend(
				{
					"label": child_label,
					"url": child_url,
					"parent_label": label,
					"right": 0,
				}
				for child_label, child_url in SERVICE_NAV
			)
			continue
		parent_id = category_ids_by_name.get(label)
		if parent_id is None:
			continue
		menu_order = {
			name.casefold(): index
			for index, name in enumerate(MENU_CATEGORY_ORDER.get(label, []))
		}
		children = sorted(
			(
				category
				for category in categories.values()
				if int(category.get("parent") or 0) == parent_id
			),
			key=lambda category: (
				menu_order.get(clean_text(category["name"]).casefold(), len(menu_order)),
				clean_text(category["name"]).casefold(),
			),
		)
		for child in children:
			child_label = group_names[int(child["id"])]
			rows.append(
				{
					"label": clean_text(child["name"]),
					"url": f"/shop?item_group={quote_query(child_label)}",
					"parent_label": label,
					"right": 0,
				}
			)

	return rows


def build_homepage_html(
	sections: dict[str, list[dict[str, Any]]],
	categories: list[dict[str, Any]],
) -> str:
	category_cards = "\n".join(category_card(category) for category in categories[:8])
	product_sections = "\n".join(product_section(title, products) for title, products in sections.items() if products)
	hero_image = next(
		(
			product["image"]
			for products in sections.values()
			for product in products
			if product.get("image")
		),
		"",
	)
	return f"""
<style>{storefront_css()}</style>
<div class="th-store">
	<header class="th-hero" style="--th-hero-image: url('{html.escape(hero_image)}')">
		<div class="th-nav">
			<div class="th-brand">Tagoury's House</div>
			<nav>{nav_links(MAIN_NAV)}</nav>
		</div>
		<div class="th-hero-content">
			<p class="th-kicker">Furniture, flooring and curated living pieces</p>
			<h1>Timeless interiors, crafted for modern homes.</h1>
			<p>Explore indoor, outdoor, lighting, rugs and limited edition collections selected from Tagoury's House.</p>
			<a class="th-button" href="/shop">Shop Collection</a>
		</div>
	</header>
	<section class="th-section th-categories">
		<div class="th-section-head">
			<h2>Shop by Category</h2>
			<a href="/shop">View all</a>
		</div>
		<div class="th-category-grid">{category_cards}</div>
	</section>
	{product_sections}
	{footer_html()}
</div>
"""


def product_section(title: str, products: list[dict[str, Any]]) -> str:
	cards = "\n".join(product_card(product) for product in products)
	return f"""
<section class="th-section">
	<div class="th-section-head">
		<h2>{html.escape(title)}</h2>
		<a href="/shop">View all</a>
	</div>
	<div class="th-product-grid">{cards}</div>
</section>
"""


def product_card(product: dict[str, Any]) -> str:
	price = format_price(product["sale_price"], product["currency"])
	regular = format_price(product["regular_price"], product["currency"])
	regular_html = f'<span class="th-regular">{regular}</span>' if product["on_sale"] else ""
	badge = '<span class="th-badge">Sale</span>' if product["on_sale"] else ""
	return f"""
<a class="th-card" href="/{html.escape(product['route'])}">
	<div class="th-image-wrap">
		{badge}
		<img src="{html.escape(product['image'] or '')}" alt="{html.escape(product['name'])}" loading="lazy">
	</div>
	<div class="th-card-body">
		<p>{html.escape(product['item_group'])}</p>
		<h3>{html.escape(product['name'])}</h3>
		<div class="th-price">{regular_html}<span>{price}</span></div>
	</div>
</a>
"""


def category_card(category: dict[str, Any]) -> str:
	name = clean_text(category["name"])
	return f"""
<a class="th-category" href="/shop?item_group={html.escape(quote_query(name))}">
	<span>{html.escape(name)}</span>
	<small>{int(category.get("count") or 0)} pieces</small>
</a>
"""


def footer_html() -> str:
	return f"""
<footer class="th-footer">
	<div>
		<h2>Subscribe Our Newsletter</h2>
		<p>Discover our stories, collections and surprises</p>
		<form><input type="email" placeholder="Your Email Address"><button>Subscribe Now</button></form>
	</div>
	<div>
		<h2>Keep In Touch</h2>
		<p>{PHONE_NUMBERS[0]}</p>
		<p>{PHONE_NUMBERS[1]}</p>
	</div>
	<div>
		<h2>Be A Partner</h2>
		{nav_links(FOOTER_LINKS)}
	</div>
</footer>
"""


def nav_links(links: list[tuple[str, str]]) -> str:
	return "".join(f'<a href="{html.escape(url)}">{html.escape(label)}</a>' for label, url in links)


def storefront_css() -> str:
	return """
.page_content, .web-page-content { max-width: none !important; padding: 0 !important; }
.th-store { color: #151515; background: #f7f5f0; font-family: Inter, Arial, sans-serif; margin: -20px calc(50% - 50vw) 0; }
.th-hero { min-height: 82vh; background: linear-gradient(90deg, rgba(0,0,0,.72), rgba(0,0,0,.22)), var(--th-hero-image); background-color: #111; background-size: cover; background-position: center; color: #fff; display: flex; flex-direction: column; }
.th-nav { display: flex; align-items: center; justify-content: space-between; gap: 24px; padding: 26px clamp(18px, 5vw, 72px); border-bottom: 1px solid rgba(255,255,255,.16); }
.th-brand { font-family: Georgia, serif; font-size: 28px; font-weight: 700; }
.th-nav nav { display: flex; gap: 22px; flex-wrap: wrap; justify-content: flex-end; }
.th-nav a, .th-footer a { color: inherit; text-decoration: none; text-transform: uppercase; font-size: 13px; letter-spacing: .04em; }
.th-hero-content { width: min(760px, 92vw); margin: auto 0; padding: 72px clamp(18px, 5vw, 72px); }
.th-kicker { text-transform: uppercase; letter-spacing: .16em; font-size: 13px; }
.th-hero h1 { color: #fff; font-family: Georgia, serif; font-size: clamp(42px, 6vw, 82px); line-height: .98; margin: 16px 0; }
.th-hero p { font-size: 18px; max-width: 620px; }
.th-button, .th-footer button { display: inline-flex; align-items: center; justify-content: center; min-height: 48px; padding: 0 24px; background: #b50020; color: #fff; text-decoration: none; text-transform: uppercase; font-weight: 700; border: 0; }
.th-section { padding: 64px clamp(18px, 5vw, 72px); }
.th-section-head { display: flex; justify-content: space-between; align-items: end; gap: 20px; margin-bottom: 24px; }
.th-section h2, .th-footer h2 { font-family: Georgia, serif; font-size: clamp(28px, 3vw, 42px); margin: 0; }
.th-section-head a { color: #111; text-transform: uppercase; font-weight: 700; }
.th-category-grid, .th-product-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(220px, 1fr)); gap: 18px; }
.th-category { min-height: 124px; background: #fff; border: 1px solid #e2ded5; padding: 22px; display: flex; flex-direction: column; justify-content: space-between; color: #111; text-decoration: none; }
.th-category span { font-size: 22px; font-weight: 700; }
.th-category small { color: #706b62; }
.th-card { background: #fff; color: #111; text-decoration: none; border: 1px solid #e2ded5; display: flex; flex-direction: column; min-width: 0; }
.th-image-wrap { position: relative; aspect-ratio: 1 / 1; background: #eee9df; overflow: hidden; }
.th-image-wrap img { width: 100%; height: 100%; object-fit: cover; display: block; transition: transform .35s ease; }
.th-card:hover img { transform: scale(1.04); }
.th-badge { position: absolute; top: 12px; left: 12px; z-index: 1; background: #b50020; color: #fff; padding: 6px 10px; text-transform: uppercase; font-size: 12px; font-weight: 700; }
.th-card-body { padding: 16px; }
.th-card-body p { color: #706b62; margin: 0 0 8px; font-size: 13px; text-transform: uppercase; }
.th-card-body h3 { font-size: 17px; line-height: 1.3; min-height: 44px; margin: 0 0 12px; }
.th-price { display: flex; gap: 10px; align-items: center; font-weight: 800; }
.th-regular { color: #8c867b; text-decoration: line-through; font-weight: 500; }
.th-footer { background: #101010; color: #fff; display: grid; grid-template-columns: minmax(260px, 2fr) 1fr 1fr; gap: 48px; padding: 52px clamp(18px, 5vw, 72px); }
.th-footer form { display: flex; gap: 16px; margin-top: 24px; }
.th-footer input { min-height: 52px; flex: 1; padding: 0 18px; border: 1px solid #ddd; }
.th-footer div:last-child { display: flex; flex-direction: column; gap: 14px; }
@media (max-width: 760px) { .th-nav { align-items: flex-start; flex-direction: column; } .th-footer { grid-template-columns: 1fr; } .th-footer form { flex-direction: column; } }
"""


def about_html() -> str:
	return simple_page_html(
		"About Us",
		"Blending timeless craftsmanship with modern sophistication, Tagoury's House creates exceptional furniture and flooring designed to elevate every space.",
	)


def story_html() -> str:
	return simple_page_html(
		"Our Story",
		"For over a century, Tagoury's House has shaped refined living experiences through exceptional furniture and flooring craftsmanship. Established in 1910 as a family business, the company continues to combine heritage, quality and modern design.",
	)


def policy_html() -> str:
	return simple_page_html(
		"Return & Exchange Policy",
		"Please contact Tagoury's House support for return and exchange requests. Items must be reviewed according to product condition, delivery status and company policy.",
	)


def vip_projects_html() -> str:
	return simple_page_html(
		"VIP Projects",
		"Dedicated furniture, flooring and design solutions for premium residential, hospitality and commercial projects.",
	)


def services_html() -> str:
	services = [
		("feng-shui", "Feng Shui Service", "Interior planning focused on balance, flow and harmony."),
		(
			"boutique-hotel",
			"Boutique Hotel Service",
			"Distinctive furniture and styling solutions for boutique hospitality spaces.",
		),
		(
			"hotel-furniture",
			"Hotel Furniture Services",
			"Furniture packages for guest rooms, public areas and hospitality projects.",
		),
		(
			"linens-catalog",
			"Linens Catalog",
			"Curated linen options for selected residential and hospitality projects.",
		),
	]
	sections = "\n".join(
		f'<section id="{anchor}" class="th-section"><h2>{html.escape(title)}</h2>'
		f"<p>{html.escape(description)}</p></section>"
		for anchor, title, description in services
	)
	return f"""
<style>{storefront_css()}</style>
<div class="th-store">
	<header class="th-hero" style="min-height: 46vh">
		<div class="th-nav"><div class="th-brand">Tagoury's House</div><nav>{nav_links(MAIN_NAV)}</nav></div>
		<div class="th-hero-content"><h1>Our Services</h1><p>Specialist support for refined residential and hospitality spaces.</p></div>
	</header>
	{sections}
	{footer_html()}
</div>
"""


def simple_page_html(title: str, text: str) -> str:
	return f"""
<style>{storefront_css()}</style>
<div class="th-store">
	<header class="th-hero" style="min-height: 46vh">
		<div class="th-nav"><div class="th-brand">Tagoury's House</div><nav>{nav_links(MAIN_NAV)}</nav></div>
		<div class="th-hero-content"><h1>{html.escape(title)}</h1><p>{html.escape(text)}</p></div>
	</header>
	{footer_html()}
</div>
"""


def product_price_info(product: dict[str, Any]) -> dict[str, Any]:
	prices = product.get("prices") or {}
	price = to_number(prices.get("price"))
	regular = to_number(prices.get("regular_price"))
	sale = to_number(prices.get("sale_price")) or price
	on_sale = bool(product.get("on_sale")) and regular is not None and sale is not None and regular > sale
	return {
		"regular_price": regular or sale,
		"sale_price": sale,
		"on_sale": on_sale,
		"currency": prices.get("currency_code") or "EGP",
	}


def home_product_payload(
	product: dict[str, Any],
	item_code: str,
	item_group: str,
	price_info: dict[str, Any],
) -> dict[str, Any]:
	image = ""
	if frappe.db.exists("Item", item_code):
		image = frappe.db.get_value("Item", item_code, "image") or ""
	if not image:
		image = ((product.get("images") or [{}])[0]).get("src") or ""
	return {
		"item_code": item_code,
		"name": clean_text(product["name"]),
		"item_group": item_group,
		"route": product_route(product),
		"image": image,
		"category_slugs": [category.get("slug") for category in product.get("categories") or []],
		**price_info,
	}


def products_for_category(products: list[dict[str, Any]], slug: str) -> list[dict[str, Any]]:
	return [product for product in products if slug in product["category_slugs"]]


def product_route(product: dict[str, Any]) -> str:
	return f"products/{slugify(product.get('slug') or product['name'])}"


def slugify(value: str) -> str:
	return re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-") or "item"


def quote_query(value: str) -> str:
	return re.sub(" ", "%20", value)


def format_price(value: float | None, currency: str) -> str:
	if value is None:
		return ""
	return f"{value:,.0f} {currency}"


def meta_fields(doctype: str) -> set[str]:
	return {field.fieldname for field in frappe.get_meta(doctype).fields}


def fit_field_value(doctype: str, fieldname: str, value: Any) -> Any:
	if not isinstance(value, str):
		return value
	field = frappe.get_meta(doctype).get_field(fieldname)
	if field and field.fieldtype in {"Data", "Link", "Dynamic Link"} and field.length:
		return value[: int(field.length)]
	return value


def get_table_field(doctype: str, fieldnames: list[str], label: str | None = None) -> Any | None:
	meta = frappe.get_meta(doctype)
	for field in meta.fields:
		if field.fieldtype == "Table" and field.fieldname in fieldnames:
			return field
	if label:
		for field in meta.fields:
			if field.fieldtype == "Table" and clean_text(field.label or "").casefold() == label.casefold():
				return field
	return None


def set_child_value(
	row: dict[str, Any],
	doctype: str,
	fieldnames: list[str],
	label: str,
	value: Any,
) -> None:
	meta = frappe.get_meta(doctype)
	for fieldname in fieldnames:
		if meta.has_field(fieldname):
			row[fieldname] = value
			return
	for field in meta.fields:
		if clean_text(field.label or "").casefold() == label.casefold():
			row[field.fieldname] = value
			return
