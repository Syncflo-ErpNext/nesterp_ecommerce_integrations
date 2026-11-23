from typing import Any

import frappe
from frappe import _
from frappe.utils import cstr, validate_phone_number
from shopify.collection import PaginatedIterator
from shopify.resources import Customer
from shopify.session import Session

from ecommerce_integrations.controllers.customer import EcommerceCustomer
from ecommerce_integrations.shopify.connection import _get_access_token
from ecommerce_integrations.shopify.constants import (
	ADDRESS_ID_FIELD,
	API_VERSION,
	CUSTOMER_ID_FIELD,
	MODULE_NAME,
	SETTING_DOCTYPE,
)
from ecommerce_integrations.shopify.utils import create_shopify_log

LOAD_CUSTOMERS_METHOD = "ecommerce_integrations.shopify.customer.load_shopify_customers"


class ShopifyCustomer(EcommerceCustomer):
	def __init__(self, customer_id: str):
		self.setting = frappe.get_doc(SETTING_DOCTYPE)
		super().__init__(customer_id, CUSTOMER_ID_FIELD, MODULE_NAME)

	def sync_customer(self, customer: dict[str, Any]) -> None:
		"""Create Customer in ERPNext using shopify's Customer dict."""

		customer_name = cstr(customer.get("first_name")) + " " + cstr(customer.get("last_name"))
		if len(customer_name.strip()) == 0:
			customer_name = customer.get("email")

		customer_group = self.setting.customer_group
		super().sync_customer(customer_name, customer_group)

		billing_address = customer.get("billing_address", {}) or customer.get("default_address")
		shipping_address = customer.get("shipping_address", {})

		if billing_address:
			self.create_customer_address(
				customer_name, billing_address, address_type="Billing", email=customer.get("email")
			)
		if shipping_address:
			self.create_customer_address(
				customer_name, shipping_address, address_type="Shipping", email=customer.get("email")
			)

		self.create_customer_contact(customer)

	def create_customer_address(
		self,
		customer_name,
		shopify_address: dict[str, Any],
		address_type: str = "Billing",
		email: str | None = None,
	) -> None:
		"""Create customer address(es) using Customer dict provided by shopify."""
		address_fields = _map_address_fields(shopify_address, customer_name, address_type, email)
		super().create_customer_address(address_fields)

	def update_existing_addresses(self, customer):
		billing_address = customer.get("billing_address", {}) or customer.get("default_address")
		shipping_address = customer.get("shipping_address", {})

		customer_name = cstr(customer.get("first_name")) + " " + cstr(customer.get("last_name"))
		email = customer.get("email")

		if billing_address:
			self._update_existing_address(customer_name, billing_address, "Billing", email)
		if shipping_address:
			self._update_existing_address(customer_name, shipping_address, "Shipping", email)

	def _update_existing_address(
		self,
		customer_name,
		shopify_address: dict[str, Any],
		address_type: str = "Billing",
		email: str | None = None,
	) -> None:
		# Only an address this connector made (it carries a Shopify Address Id) is ever updated. A customer
		# linked by email keeps their own addresses, e.g. one made at a store till; Shopify's is added beside it.
		old_address = None
		made_here = frappe.get_all(
			"Address",
			filters={
				"link_doctype": "Customer",
				"link_name": self.get_customer_doc().name,
				"address_type": address_type,
				ADDRESS_ID_FIELD: ["is", "set"],
			},
			pluck="name",
			order_by="creation desc",
			limit=1,
		)
		if made_here:
			old_address = frappe.get_doc("Address", made_here[0])

		if not old_address:
			self.create_customer_address(customer_name, shopify_address, address_type, email)
		else:
			exclude_in_update = ["address_title", "address_type"]
			new_values = _map_address_fields(shopify_address, customer_name, address_type, email)

			old_address.update({k: v for k, v in new_values.items() if k not in exclude_in_update})
			old_address.flags.ignore_mandatory = True
			old_address.save()

	def create_customer_contact(self, shopify_customer: dict[str, Any]) -> None:
		email = shopify_customer.get("email")
		phone_no = shopify_customer.get("phone") or (shopify_customer.get("default_address") or {}).get(
			"phone"
		)
		if not validate_phone_number(phone_no, throw=False):
			phone_no = None

		contact = None
		if shopify_customer.get("first_name") and email:
			contact_fields = {
				"status": "Passive",
				"first_name": shopify_customer.get("first_name"),
				"last_name": shopify_customer.get("last_name"),
				"is_primary_contact": 1,
				"unsubscribed": not _accepts_marketing(shopify_customer),
				"email_ids": [{"email_id": email, "is_primary": True}],
			}
			if phone_no:
				contact_fields["phone_nos"] = [{"phone": phone_no, "is_primary_phone": True}]

			contact = super().create_customer_contact(contact_fields)

		# email on the customer itself, so this buyer is found by email next time
		values = {"email_id": email, "mobile_no": phone_no}
		if contact:
			values["customer_primary_contact"] = contact.name
		frappe.db.set_value("Customer", self.get_customer_doc().name, values)

	def link_existing_customer(self, email: str | None) -> bool:
		"""Link this Shopify customer to the first unlinked ERPNext customer with the same email
		in one of the setting's match groups. Returns True when linked."""
		groups = [
			g.strip() for g in cstr(self.setting.get("customer_match_groups")).splitlines() if g.strip()
		]
		if not (groups and email):
			return False

		existing = frappe.get_all(
			"Customer",
			filters={
				"email_id": email.strip(),
				"customer_group": ["in", groups],
				"disabled": 0,
				CUSTOMER_ID_FIELD: ["is", "not set"],
			},
			pluck="name",
			order_by="creation asc",
			limit=1,
		)
		if not existing:
			return False

		frappe.db.set_value("Customer", existing[0], CUSTOMER_ID_FIELD, self.customer_id)
		return True

	def record_marketing_consent(self) -> None:
		"""Tick the configured Customer check field (e.g. a mailing-list box); never untick it."""
		fieldname = cstr(self.setting.get("marketing_consent_field")).strip()
		if not fieldname or not frappe.get_meta("Customer").has_field(fieldname):
			return

		customer = self.get_customer_doc()
		if customer.get(fieldname):
			return

		customer.set(fieldname, 1)
		customer.flags.ignore_mandatory = True
		customer.save(ignore_permissions=True)  # a save, not db_set: hooks on Customer must run


def _accepts_marketing(shopify_customer: dict[str, Any]) -> bool:
	"""Shopify's email marketing consent (current API), or the old accepts_marketing flag."""
	consent = shopify_customer.get("email_marketing_consent") or {}
	return consent.get("state") == "subscribed" or bool(shopify_customer.get("accepts_marketing"))


@frappe.whitelist()
def load_shopify_customers() -> str:
	"""Start the one-off load of every Shopify customer; returns the log that will hold the result."""
	frappe.only_for("System Manager")

	log = create_shopify_log(
		status="Queued",
		method=LOAD_CUSTOMERS_METHOD,
		message=_("Loading Shopify customers"),
		make_new=True,
	)
	frappe.enqueue(
		"ecommerce_integrations.shopify.customer._load_shopify_customers",
		queue="long",
		timeout=4 * 60 * 60,
		job_id="shopify_load_customers",
		deduplicate=True,
		enqueue_after_commit=True,
		log_name=log.name,
	)
	return log.name


def _load_shopify_customers(log_name: str) -> None:
	"""Link each Shopify customer to an ERPNext customer by email (match groups) or create one.

	Works whether or not the integration is switched on, so it can run before go-live.
	Commits per customer: one bad record never undoes the others.
	"""
	frappe.set_user("Administrator")
	frappe.flags.request_id = log_name
	counts = {"already linked": 0, "linked by email": 0, "created": 0, "skipped (no email)": 0, "failed": 0}
	failures = []

	try:
		setting = frappe.get_doc(SETTING_DOCTYPE)
		with Session.temp(setting.shopify_url, API_VERSION, _get_access_token(setting)):
			for page in PaginatedIterator(Customer.find(limit=250)):
				for record in page:
					data = record.to_dict()
					_load_one_customer(data, counts, failures)
	except Exception as e:
		create_shopify_log(status="Error", exception=e, message=_summary(counts, failures, str(e)))
		return

	create_shopify_log(status="Success", message=_summary(counts, failures))


def _load_one_customer(data: dict[str, Any], counts: dict[str, int], failures: list[str]) -> None:
	customer_id = cstr(data.get("id"))
	email = cstr(data.get("email")).strip()
	try:
		customer = ShopifyCustomer(customer_id=customer_id)
		if customer.is_synced():
			counts["already linked"] += 1
		elif customer.link_existing_customer(email):
			counts["linked by email"] += 1
		elif not email:
			counts["skipped (no email)"] += 1
			return
		else:
			customer.sync_customer(customer=data)
			counts["created"] += 1

		if _accepts_marketing(data):
			customer.record_marketing_consent()
		frappe.db.commit()
	except Exception as e:
		frappe.db.rollback()
		counts["failed"] += 1
		failures.append(f"{customer_id} {email}: {cstr(e)[:200]}")


def _summary(counts: dict[str, int], failures: list[str], error: str | None = None) -> str:
	lines = [_("Shopify customers loaded:")] + [f"{label}: {n}" for label, n in counts.items()]
	if error:
		lines.append(_("Stopped early: {0}").format(error))
	if failures:
		lines.append(_("Failed (first 50):"))
		lines.extend(failures[:50])
	return "\n".join(lines)


def _map_address_fields(shopify_address, customer_name, address_type, email):
	"""returns dict with shopify address fields mapped to equivalent ERPNext fields"""
	address_fields = {
		"address_title": customer_name,
		"address_type": address_type,
		# an order's addresses have no id in Shopify; "order" still marks the address as made by this connector
		ADDRESS_ID_FIELD: shopify_address.get("id") or "order",
		"address_line1": shopify_address.get("address1") or "Address 1",
		"address_line2": shopify_address.get("address2"),
		"city": shopify_address.get("city"),
		"state": shopify_address.get("province"),
		"pincode": shopify_address.get("zip"),
		"country": shopify_address.get("country"),
		"email_id": email,
	}

	phone = shopify_address.get("phone")
	if validate_phone_number(phone, throw=False):
		address_fields["phone"] = phone

	return address_fields
