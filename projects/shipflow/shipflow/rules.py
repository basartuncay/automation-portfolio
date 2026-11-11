"""Which carrier and service an order gets, and what has to be fixed before it can ship. Every decision carries the
rule that made it, so the warehouse can see why an order went DPD and not DHL, or why it's on hold."""
import re

EU = {"AT", "BE", "BG", "CY", "CZ", "DE", "DK", "EE", "ES", "FI", "FR", "GR", "HR", "HU", "IE", "IT", "LT", "LU", "LV", "MT", "NL", "PL", "PT", "RO", "SE", "SI", "SK"}
POSTCODE = {"DE": r"\d{5}", "AT": r"\d{4}", "CH": r"\d{4}", "NL": r"\d{4}\s?[A-Z]{2}", "FR": r"\d{5}", "BE": r"\d{4}", "IT": r"\d{5}", "ES": r"\d{5}",
            "PL": r"\d{2}-\d{3}", "GB": r"[A-Z]{1,2}\d[A-Z\d]?\s?\d[A-Z]{2}", "US": r"\d{5}(-\d{4})?"}
MAX_PARCEL_G = 31_500


def check_address(a: dict) -> list[str]:
    problems = []
    for f, label in (("name", "name"), ("street", "street"), ("city", "city"), ("postcode", "postcode"), ("country", "country")):
        if not (a.get(f) or "").strip():
            problems.append(f"{label} is missing")
    street, country = (a.get("street") or "").strip(), (a.get("country") or "").upper()
    if street and not re.search(r"\d", street) and not re.search(r"packstation|postfiliale", street, re.I):
        problems.append("street has no house number")
    pattern = POSTCODE.get(country)
    if pattern and a.get("postcode") and not re.fullmatch(pattern, a["postcode"].strip().upper()):
        problems.append(f"postcode {a['postcode']!r} isn't a valid {country} postcode")
    if country not in EU and not (a.get("phone") or "").strip():
        problems.append("a phone number is needed for deliveries outside the EU")
    return problems


def decide(order: dict, products: dict) -> dict:
    """{'carrier', 'service', 'rule', 'weight_g'} or {'hold': reason}."""
    a = order["address"]
    country = (a.get("country") or "").upper()
    problems = check_address(a)
    unknown = [l["sku"] for l in order["lines"] if l["sku"] not in products]
    if unknown:
        problems.append(f"unknown SKU(s): {', '.join(unknown)}")
    if problems:
        return {"hold": "Address or order needs fixing: " + "; ".join(problems)}
    weight = sum(products[l["sku"]]["weight_g"] * l["qty"] for l in order["lines"]) + 300  # + packaging
    if weight > MAX_PARCEL_G:
        return {"hold": f"{weight / 1000:.1f} kg is over the {MAX_PARCEL_G / 1000:.1f} kg parcel limit: book freight by hand", "weight_g": weight}
    packstation = bool(re.search(r"packstation", a.get("street", ""), re.I))
    if country not in EU:
        missing = sorted({l["sku"] for l in order["lines"] if not products[l["sku"]]["hs_code"]})
        if missing:
            return {"hold": f"customs: no HS code for {', '.join(missing)}", "weight_g": weight}
        return {"carrier": "dhl_express", "service": "Worldwide (with customs CN23)", "rule": "outside the EU: DHL Express with customs data", "weight_g": weight}
    if packstation:
        return {"carrier": "dhl", "service": "Paket to Packstation", "rule": "Packstation address: only DHL delivers there", "weight_g": weight}
    if order.get("shipping") == "express":
        return {"carrier": "dhl_express", "service": "Express next day", "rule": "customer paid for express", "weight_g": weight}
    if country == "DE":
        return {"carrier": "dhl", "service": "Paket" if weight > 1000 else "Warenpost", "rule": f"Germany, {weight / 1000:.1f} kg: DHL {'Paket' if weight > 1000 else 'Warenpost (under 1 kg)'}", "weight_g": weight}
    return {"carrier": "dpd", "service": "Classic EU", "rule": f"EU ({country}): DPD Classic is cheapest", "weight_g": weight}
