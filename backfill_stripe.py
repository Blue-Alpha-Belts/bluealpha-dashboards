#!/usr/bin/env python3
"""
Backfill Stripe CC + ACH invoices for all approved invoices missing Stripe links.
Does NOT send any emails to customers.
Run from the bluealpha-dashboards directory.
"""

import os, sys, json, time
import requests

AIRTABLE_TOKEN  = os.environ.get("AIRTABLE_TOKEN", "")
WRITE_TOKEN     = os.environ.get("AIRTABLE_WRITE_TOKEN", "")
STRIPE_KEY      = os.environ.get("STRIPE_SECRET_KEY", "")
BASE_ID         = "appA13jo4b3TIn4yT"
ORDERS_TABLE    = "tblOOZ2wVzIsR1DyL"
LI_TABLE        = "tblNDxbfgyZDMex7n"  # MO Line Items

AT_HEADERS  = {"Authorization": f"Bearer {AIRTABLE_TOKEN}"}
AT_WRITE_HEADERS = {"Authorization": f"Bearer {WRITE_TOKEN}", "Content-Type": "application/json"}

SS_AUTH     = (STRIPE_KEY, "")

def at_get_all(table, fields=None, formula=None):
    records, offset = [], None
    while True:
        params = {"pageSize": 100}
        if fields:
            for i, f in enumerate(fields): params[f"fields[{i}]"] = f
        if formula: params["filterByFormula"] = formula
        if offset:  params["offset"] = offset
        r = requests.get(f"https://api.airtable.com/v0/{BASE_ID}/{table}",
                         headers=AT_HEADERS, params=params, timeout=30)
        r.raise_for_status()
        d = r.json()
        records.extend(d.get("records", []))
        offset = d.get("offset")
        if not offset: break
    return records

def create_stripe_customer(email, name, org):
    """Always create a fresh Stripe customer (matches Flask app behavior)."""
    # Stripe only accepts a single email — take first if semicolon/comma-separated
    stripe_email = email.replace(",", ";").split(";")[0].strip()
    r = requests.post("https://api.stripe.com/v1/customers",
                      auth=SS_AUTH,
                      data={"email": stripe_email,
                            "name": org or name or stripe_email,
                            "description": name or ""},
                      timeout=15)
    r.raise_for_status()
    cust_id = r.json()["id"]
    print(f"  → Created Stripe customer: {cust_id}")
    return cust_id

def create_stripe_invoice(cust_id, li_items, method):
    """Create and finalize a Stripe invoice for 'card' or 'us_bank_account'."""
    inv_r = requests.post("https://api.stripe.com/v1/invoices",
                          auth=SS_AUTH,
                          data={
                              "customer": cust_id,
                              "collection_method": "send_invoice",
                              "days_until_due": "30",
                              "payment_settings[payment_method_types][0]": method,
                          }, timeout=15)
    if not inv_r.ok:
        raise Exception(f"Invoice create failed ({method}): {inv_r.status_code} {inv_r.text[:300]}")
    inv_id = inv_r.json()["id"]

    for item in li_items:
        unit_cents_decimal = str(round(float(item["unit_price"]) * 100, 4))
        ii_r = requests.post("https://api.stripe.com/v1/invoiceitems",
                             auth=SS_AUTH,
                             data={
                                 "customer": cust_id,
                                 "invoice": inv_id,
                                 "description": item["name"],
                                 "quantity": str(int(item["qty"])),
                                 "unit_amount_decimal": unit_cents_decimal,
                                 "currency": "usd",
                             }, timeout=15)
        if not ii_r.ok:
            print(f"  ⚠ Line item warn: {ii_r.status_code} {ii_r.text[:200]}")

    fin_r = requests.post(f"https://api.stripe.com/v1/invoices/{inv_id}/finalize",
                          auth=SS_AUTH, data={}, timeout=15)
    if not fin_r.ok:
        raise Exception(f"Finalize failed ({method}): {fin_r.status_code} {fin_r.text[:300]}")
    url = fin_r.json().get("hosted_invoice_url", "")
    return inv_id, url

def main():
    print("Fetching invoices missing Stripe links...")
    missing = at_get_all(
        ORDERS_TABLE,
        fields=["Document ID", "Bill-To Contact Email (from Customer)",
                "Bill-To Contact Name (from Customer)", "Bill-To Org Name (from Customer)",
                "MO Line Items"],
        formula="AND({Order Type}=\"Invoice\",{Stripe Invoice URL (CC)}='',{Invoice Status}=\"Approved\")"
    )
    print(f"Found {len(missing)} invoices to backfill.\n")

    # Fetch write token from Railway env if available
    write_tok = os.environ.get("AIRTABLE_WRITE_TOKEN_2", WRITE_TOKEN)

    for rec in missing:
        f   = rec["fields"]
        rid = rec["id"]
        doc = f.get("Document ID", rid)

        def _first(lst):
            return lst[0] if isinstance(lst, list) and lst else (lst or "")

        email     = _first(f.get("Bill-To Contact Email (from Customer)", []))
        name      = _first(f.get("Bill-To Contact Name (from Customer)", []))
        org       = _first(f.get("Bill-To Org Name (from Customer)", []))
        li_ids    = f.get("MO Line Items", [])

        print(f"Processing {doc} — {org or email}")

        if not email:
            print(f"  ⚠ No billing email, skipping.")
            continue

        # Fetch line items (no fields filter — Airtable 422s on fields[] for single-record GETs)
        li_items = []
        for li_id in li_ids:
            lr = requests.get(f"https://api.airtable.com/v0/{BASE_ID}/{LI_TABLE}/{li_id}",
                              headers=AT_HEADERS, timeout=10)
            if not lr.ok:
                print(f"  ⚠ Could not fetch line item {li_id}: {lr.status_code}")
                continue
            lf = lr.json().get("fields", {})
            price = float(lf.get("Confirmed Unit Price") or 0)
            qty   = int(lf.get("Qty.") or 0)
            pname_raw = lf.get("Name + Variations (from Product SKU)") or lf.get("Product Name (from Product SKU)", [])
            pname = pname_raw[0] if isinstance(pname_raw, list) and pname_raw else str(pname_raw or "Item")
            if price > 0 and qty > 0:
                li_items.append({"name": pname, "qty": qty, "unit_price": price})

        if not li_items:
            print(f"  ⚠ No line items with price, skipping.")
            continue

        try:
            cust_id = create_stripe_customer(email, name, org)

            cc_id,  cc_url  = create_stripe_invoice(cust_id, li_items, "card")
            ach_id, ach_url = create_stripe_invoice(cust_id, li_items, "us_bank_account")

            # Patch Airtable (no email sent — just writing URLs)
            patch = {
                "fields": {
                    "Stripe Customer ID":          cust_id,
                    "Stripe Invoice ID (CC)":      cc_id,
                    "Stripe Invoice URL (CC)":     cc_url,
                    "Stripe Invoice Status (CC)":  "Open",
                    "Stripe Invoice ID (ACH)":     ach_id,
                    "Stripe Invoice URL (ACH)":    ach_url,
                    "Stripe Invoice Status (ACH)": "Open",
                }
            }
            pr = requests.patch(
                f"https://api.airtable.com/v0/{BASE_ID}/{ORDERS_TABLE}/{rid}",
                headers=AT_WRITE_HEADERS,
                json=patch, timeout=15
            )
            if pr.ok:
                print(f"  ✓ Done — CC: {cc_url[:60]}...")
            else:
                print(f"  ✗ Airtable patch failed: {pr.status_code} {pr.text[:200]}")

        except Exception as e:
            print(f"  ✗ Error: {e}")

        time.sleep(0.5)  # gentle rate limiting

    print("\nBackfill complete.")

if __name__ == "__main__":
    main()
