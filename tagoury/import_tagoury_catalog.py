from __future__ import annotations

import argparse
import html
import json
import mimetypes
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from collections import defaultdict
from pathlib import Path
from typing import Any


SOURCE_BASE_URL = "https://tagouryshouse.com"
STORE_API_BASE = f"{SOURCE_BASE_URL}/wp-json/wc/store/v1"
DEFAULT_TARGET_URL = "https://tagoury.connect4systems.com"
ROOT_ITEM_GROUP = "Tagoury's House"
PROMOTIONAL_GROUP_SLUGS = {
	"best-selling",
	"cottage",
	"indian-line",
	"limited-editions",
	"new-collection",
	"offers",
}


def import_catalog(
	target_url: str | None = None,
	api_key: str | None = None,
	api_secret: str | None = None,
	export_path: str | None = None,
	limit: int | None = None,
	dry_run: bool = False,
	create_item_prices: bool = True,
) -> dict[str, Any]:
	"""Import Tagoury's House WooCommerce catalog into ERPNext.

	When called from `bench execute`, no API key is needed because the function
	uses the current Frappe site. Outside a bench, pass `target_url`, `api_key`,
	and `api_secret` to use ERPNext's REST API.
	"""
	catalog = fetch_catalog(limit=limit)
	if export_path:
		write_json(export_path, catalog)

	report: dict[str, Any] = {
		"source": SOURCE_BASE_URL,
		"groups_fetched": len(catalog["categories"]),
		"items_fetched": len(catalog["products"]),
		"groups_created": 0,
		"groups_updated": 0,
		"items_created": 0,
		"items_updated": 0,
		"item_prices_created": 0,
		"images_uploaded": 0,
		"skipped": [],
	}

	if dry_run:
		report["dry_run"] = True
		return report

	client = get_client(target_url=target_url, api_key=api_key, api_secret=api_secret)
	import_to_erpnext(client, catalog, report, create_item_prices=create_item_prices)
	return report


def fetch_catalog(limit: int | None = None) -> dict[str, Any]:
	categories = get_json(f"{STORE_API_BASE}/products/categories")
	products: list[dict[str, Any]] = []
	page = 1
	while True:
		batch = get_json(f"{STORE_API_BASE}/products?per_page=100&page={page}")
		if not batch:
			break
		products.extend(batch)
		if limit and len(products) >= limit:
			products = products[:limit]
			break
		page += 1
		time.sleep(0.1)

	return {
		"fetched_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
		"categories": categories,
		"products": products,
	}


def import_to_erpnext(
	client: "BaseClient",
	catalog: dict[str, Any],
	report: dict[str, Any],
	create_item_prices: bool,
) -> None:
	categories = {category["id"]: category for category in catalog["categories"]}
	children_by_parent: dict[int, list[dict[str, Any]]] = defaultdict(list)
	for category in categories.values():
		children_by_parent[int(category.get("parent") or 0)].append(category)

	group_names: dict[int, str] = {}
	ensure_group(client, ROOT_ITEM_GROUP, "All Item Groups", report, is_group=True)

	def sync_category(category: dict[str, Any]) -> str:
		category_id = int(category["id"])
		if category_id in group_names:
			return group_names[category_id]

		parent_id = int(category.get("parent") or 0)
		parent_group = ROOT_ITEM_GROUP if not parent_id else sync_category(categories[parent_id])
		group_name = unique_group_name(category, categories)
		ensure_group(
			client,
			group_name,
			parent_group,
			report,
			category,
			is_group=bool(children_by_parent.get(category_id)),
		)
		group_names[category_id] = group_name
		return group_name

	for category in sorted(categories.values(), key=lambda item: category_depth(item, categories)):
		sync_category(category)

	for product in catalog["products"]:
		sync_product(client, product, categories, group_names, report, create_item_prices)


def sync_product(
	client: "BaseClient",
	product: dict[str, Any],
	categories: dict[int, dict[str, Any]],
	group_names: dict[int, str],
	report: dict[str, Any],
	create_item_prices: bool,
) -> None:
	sku = clean_text(product.get("sku") or "")
	if not sku:
		sku = f"TH-{product['id']}"

	item_group = choose_item_group(product, categories, group_names)
	description = build_item_description(product)
	image_url = (product.get("images") or [{}])[0].get("src") or ""
	image_file_url = ""

	doc = {
		"doctype": "Item",
		"item_code": sku,
		"item_name": clean_text(product["name"]),
		"item_group": item_group,
		"stock_uom": "Nos",
		"is_stock_item": 1,
		"include_item_in_manufacturing": 0,
		"description": description,
	}
	if image_file_url:
		doc["image"] = image_file_url

	price = to_number(((product.get("prices") or {}).get("price")))
	if price is not None:
		doc["standard_rate"] = price

	if client.exists("Item", sku):
		client.update("Item", sku, doc)
		report["items_updated"] += 1
	else:
		client.create("Item", doc)
		report["items_created"] += 1

	if image_url:
		try:
			image_file_url = client.upload_file(image_url, product["name"], "Item", sku)
			if image_file_url:
				client.update("Item", sku, {"doctype": "Item", "image": image_file_url})
			report["images_uploaded"] += 1
		except Exception as exc:
			report["skipped"].append({"item": sku, "reason": f"image upload failed: {exc}"})

	if create_item_prices and price is not None:
		price_doc = {
			"doctype": "Item Price",
			"item_code": sku,
			"price_list": "Standard Selling",
			"selling": 1,
			"currency": (product.get("prices") or {}).get("currency_code") or "EGP",
			"price_list_rate": price,
		}
		price_name = client.find_one(
			"Item Price",
			{"item_code": sku, "price_list": "Standard Selling", "selling": 1},
		)
		if price_name:
			client.update("Item Price", price_name, price_doc)
		else:
			client.create("Item Price", price_doc)
			report["item_prices_created"] += 1


def ensure_group(
	client: "BaseClient",
	group_name: str,
	parent_group: str,
	report: dict[str, Any],
	category: dict[str, Any] | None = None,
	is_group: bool = True,
) -> None:
	doc = {
		"doctype": "Item Group",
		"item_group_name": group_name,
		"parent_item_group": parent_group,
		"is_group": 1 if is_group else 0,
	}
	if category:
		doc["show_in_website"] = 1
		if category.get("description"):
			doc["description"] = clean_html(category["description"])

	if client.exists("Item Group", group_name):
		client.update("Item Group", group_name, doc)
		report["groups_updated"] += 1
	else:
		client.create("Item Group", doc)
		report["groups_created"] += 1


def choose_item_group(
	product: dict[str, Any],
	categories: dict[int, dict[str, Any]],
	group_names: dict[int, str],
) -> str:
	product_categories = [categories[item["id"]] for item in product.get("categories", []) if item["id"] in categories]
	non_promotional = [
		category for category in product_categories if category.get("slug") not in PROMOTIONAL_GROUP_SLUGS
	]
	candidates = non_promotional or product_categories
	if not candidates:
		return ROOT_ITEM_GROUP

	selected = max(candidates, key=lambda category: category_depth(category, categories))
	return group_names[int(selected["id"])]


def unique_group_name(category: dict[str, Any], categories: dict[int, dict[str, Any]]) -> str:
	name = clean_text(category["name"])
	same_named = [item for item in categories.values() if clean_text(item["name"]).casefold() == name.casefold()]
	if len(same_named) == 1:
		return name

	path = category_path(category, categories)
	prefix = " / ".join(clean_text(item["name"]) for item in path[:-1])
	return f"{name} ({prefix})" if prefix else name


def category_depth(category: dict[str, Any], categories: dict[int, dict[str, Any]]) -> int:
	return len(category_path(category, categories))


def category_path(category: dict[str, Any], categories: dict[int, dict[str, Any]]) -> list[dict[str, Any]]:
	path = [category]
	parent_id = int(category.get("parent") or 0)
	while parent_id and parent_id in categories:
		parent = categories[parent_id]
		path.insert(0, parent)
		parent_id = int(parent.get("parent") or 0)
	return path


def build_item_description(product: dict[str, Any]) -> str:
	parts = []
	short_description = clean_html(product.get("short_description") or "")
	description = clean_html(product.get("description") or "")
	if short_description:
		parts.append(short_description)
	if description and description != short_description:
		parts.append(description)

	attributes = []
	for attribute in product.get("attributes") or []:
		terms = ", ".join(clean_text(term["name"]) for term in attribute.get("terms") or [])
		if terms:
			attributes.append(f"{clean_text(attribute['name'])}: {terms}")
	if attributes:
		parts.append("\n".join(attributes))

	return "\n\n".join(parts) or clean_text(product["name"])


def clean_html(value: str) -> str:
	value = html.unescape(value)
	value = re.sub(r"<\s*br\s*/?\s*>", "\n", value, flags=re.I)
	value = re.sub(r"</p\s*>", "\n", value, flags=re.I)
	value = re.sub(r"<[^>]+>", "", value)
	return clean_text(value)


def clean_text(value: Any) -> str:
	return re.sub(r"\s+", " ", html.unescape(str(value))).strip()


def to_number(value: Any) -> float | None:
	if value in (None, ""):
		return None
	try:
		return float(value)
	except (TypeError, ValueError):
		return None


def get_json(url: str) -> Any:
	request = urllib.request.Request(url, headers={"User-Agent": "tagoury-erpnext-importer/1.0"})
	with urllib.request.urlopen(request, timeout=60) as response:
		return json.loads(response.read().decode("utf-8"))


def get_binary(url: str) -> tuple[bytes, str]:
	request = urllib.request.Request(url, headers={"User-Agent": "tagoury-erpnext-importer/1.0"})
	with urllib.request.urlopen(request, timeout=90) as response:
		content_type = response.headers.get("Content-Type") or "application/octet-stream"
		return response.read(), content_type


def write_json(path: str, data: Any) -> None:
	target = Path(path)
	target.parent.mkdir(parents=True, exist_ok=True)
	target.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def get_client(target_url: str | None, api_key: str | None, api_secret: str | None) -> "BaseClient":
	try:
		import frappe  # type: ignore

		if getattr(frappe.local, "site", None):
			return FrappeClient(frappe)
	except Exception:
		pass

	if not api_key or not api_secret:
		raise RuntimeError("Run inside bench, or pass --api-key and --api-secret for ERPNext REST import.")
	return RestClient((target_url or DEFAULT_TARGET_URL).rstrip("/"), api_key, api_secret)


class BaseClient:
	def exists(self, doctype: str, name: str) -> bool:
		raise NotImplementedError

	def find_one(self, doctype: str, filters: dict[str, Any]) -> str | None:
		raise NotImplementedError

	def create(self, doctype: str, doc: dict[str, Any]) -> dict[str, Any]:
		raise NotImplementedError

	def update(self, doctype: str, name: str, doc: dict[str, Any]) -> dict[str, Any]:
		raise NotImplementedError

	def upload_file(self, source_url: str, title: str, attached_to_doctype: str, attached_to_name: str) -> str:
		raise NotImplementedError


class FrappeClient(BaseClient):
	def __init__(self, frappe_module: Any) -> None:
		self.frappe = frappe_module

	def exists(self, doctype: str, name: str) -> bool:
		return bool(self.frappe.db.exists(doctype, name))

	def find_one(self, doctype: str, filters: dict[str, Any]) -> str | None:
		return self.frappe.db.get_value(doctype, filters, "name")

	def create(self, doctype: str, doc: dict[str, Any]) -> dict[str, Any]:
		record = self.frappe.get_doc(doc)
		record.insert(ignore_permissions=True)
		self.frappe.db.commit()
		return record.as_dict()

	def update(self, doctype: str, name: str, doc: dict[str, Any]) -> dict[str, Any]:
		record = self.frappe.get_doc(doctype, name)
		for field, value in doc.items():
			if field != "doctype":
				record.set(field, value)
		record.save(ignore_permissions=True)
		self.frappe.db.commit()
		return record.as_dict()

	def upload_file(self, source_url: str, title: str, attached_to_doctype: str, attached_to_name: str) -> str:
		content, content_type = get_binary(source_url)
		file_name = image_filename(source_url, title, content_type)
		existing = self.frappe.db.get_value(
			"File",
			{
				"attached_to_doctype": attached_to_doctype,
				"attached_to_name": attached_to_name,
				"file_name": file_name,
			},
			"file_url",
		)
		if existing:
			return existing

		file_doc = self.frappe.get_doc(
			{
				"doctype": "File",
				"file_name": file_name,
				"attached_to_doctype": attached_to_doctype,
				"attached_to_name": attached_to_name,
				"is_private": 0,
				"content": content,
			}
		)
		file_doc.save(ignore_permissions=True)
		self.frappe.db.commit()
		return file_doc.file_url


class RestClient(BaseClient):
	def __init__(self, target_url: str, api_key: str, api_secret: str) -> None:
		self.target_url = target_url
		self.headers = {
			"Authorization": f"token {api_key}:{api_secret}",
			"Accept": "application/json",
		}

	def exists(self, doctype: str, name: str) -> bool:
		try:
			self._request("GET", f"/api/resource/{quote(doctype)}/{quote(name)}")
			return True
		except urllib.error.HTTPError as exc:
			if exc.code == 404:
				return False
			raise

	def find_one(self, doctype: str, filters: dict[str, Any]) -> str | None:
		query = urllib.parse.urlencode(
			{
				"fields": json.dumps(["name"]),
				"filters": json.dumps(filters),
				"limit_page_length": "1",
			}
		)
		response = self._request("GET", f"/api/resource/{quote(doctype)}?{query}")
		data = response.get("data") or []
		return data[0]["name"] if data else None

	def create(self, doctype: str, doc: dict[str, Any]) -> dict[str, Any]:
		return self._request("POST", f"/api/resource/{quote(doctype)}", doc)

	def update(self, doctype: str, name: str, doc: dict[str, Any]) -> dict[str, Any]:
		return self._request("PUT", f"/api/resource/{quote(doctype)}/{quote(name)}", doc)

	def upload_file(self, source_url: str, title: str, attached_to_doctype: str, attached_to_name: str) -> str:
		content, content_type = get_binary(source_url)
		file_name = image_filename(source_url, title, content_type)
		fields = {
			"doctype": attached_to_doctype,
			"docname": attached_to_name,
			"is_private": "0",
			"folder": "Home/Attachments",
		}
		files = {"file": (file_name, content, content_type)}
		response = self._multipart("/api/method/upload_file", fields, files)
		return (response.get("message") or {}).get("file_url") or ""

	def _request(self, method: str, path: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
		headers = dict(self.headers)
		data = None
		if payload is not None:
			data = json.dumps(payload).encode("utf-8")
			headers["Content-Type"] = "application/json"
		request = urllib.request.Request(f"{self.target_url}{path}", data=data, headers=headers, method=method)
		with urllib.request.urlopen(request, timeout=60) as response:
			return json.loads(response.read().decode("utf-8"))

	def _multipart(
		self,
		path: str,
		fields: dict[str, str],
		files: dict[str, tuple[str, bytes, str]],
	) -> dict[str, Any]:
		boundary = f"----tagoury-{uuid.uuid4().hex}"
		body = bytearray()
		for name, value in fields.items():
			body.extend(f"--{boundary}\r\n".encode())
			body.extend(f'Content-Disposition: form-data; name="{name}"\r\n\r\n{value}\r\n'.encode())
		for name, (filename, content, content_type) in files.items():
			body.extend(f"--{boundary}\r\n".encode())
			body.extend(
				f'Content-Disposition: form-data; name="{name}"; filename="{filename}"\r\n'.encode()
			)
			body.extend(f"Content-Type: {content_type}\r\n\r\n".encode())
			body.extend(content)
			body.extend(b"\r\n")
		body.extend(f"--{boundary}--\r\n".encode())

		headers = dict(self.headers)
		headers["Content-Type"] = f"multipart/form-data; boundary={boundary}"
		request = urllib.request.Request(
			f"{self.target_url}{path}",
			data=bytes(body),
			headers=headers,
			method="POST",
		)
		with urllib.request.urlopen(request, timeout=90) as response:
			return json.loads(response.read().decode("utf-8"))


def quote(value: str) -> str:
	return urllib.parse.quote(value, safe="")


def image_filename(source_url: str, title: str, content_type: str) -> str:
	parsed = urllib.parse.urlparse(source_url)
	name = Path(urllib.parse.unquote(parsed.path)).name
	extension = mimetypes.guess_extension(content_type.split(";")[0].strip()) or ".jpg"
	if name and "." in name:
		stem = Path(name).stem
		extension = Path(name).suffix or extension
	else:
		stem = title
	slug = re.sub(r"[^a-z0-9]+", "-", stem.lower()).strip("-") or "tagoury-item"
	return f"{slug}{extension}"


def parse_args(argv: list[str]) -> argparse.Namespace:
	parser = argparse.ArgumentParser(description="Import Tagoury's House catalog into ERPNext.")
	parser.add_argument("--target-url", default=DEFAULT_TARGET_URL)
	parser.add_argument("--api-key")
	parser.add_argument("--api-secret")
	parser.add_argument("--export-path", default="data/tagoury_catalog.json")
	parser.add_argument("--limit", type=int)
	parser.add_argument("--dry-run", action="store_true")
	parser.add_argument("--no-item-prices", action="store_true")
	return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
	args = parse_args(argv or sys.argv[1:])
	report = import_catalog(
		target_url=args.target_url,
		api_key=args.api_key,
		api_secret=args.api_secret,
		export_path=args.export_path,
		limit=args.limit,
		dry_run=args.dry_run,
		create_item_prices=not args.no_item_prices,
	)
	print(json.dumps(report, indent=2, ensure_ascii=False))
	return 0


if __name__ == "__main__":
	raise SystemExit(main())
