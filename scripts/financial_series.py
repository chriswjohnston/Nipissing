#!/usr/bin/env python3
"""
financial_series.py — the numbers the Finances page charts, from the FIR.

Reads data/financial/fir/<year>.csv (Nipissing's rows of the Province's
Financial Information Return, kept by financials.py) and writes
data/canonical/financial_series.json: one record per year, each figure
named by the FIR line it came from (its SLC code), so anyone can check it.

The FIR is filed by every Ontario municipality on the same form, from the
same audited statements, so the years line up: 2009 onward (full accrual
accounting; earlier years aren't comparable). A few lines differ slightly
from how the audited statements group things (e.g. salaries in 2024); the
page says so and names the FIR as its source.
"""
from __future__ import annotations

import csv
import json
from datetime import date
from pathlib import Path
from typing import Dict

ROOT = Path(__file__).resolve().parents[1]
FIR_DIR = ROOT / "data" / "financial" / "fir"
OUT = ROOT / "data" / "canonical" / "financial_series.json"

# name: (SLC, what it is)
LINES = {
    "property_tax": ("slc.10X.L9940.C01.01", "Property taxation, municipal purposes (Schedule 10)"),
    "revenue": ("slc.10X.L9910.C01.01", "Total revenues (Schedule 10)"),
    "ompf": ("slc.10X.L0620.C01.01", "Ontario Municipal Partnership Fund (Schedule 10)"),
    "prov_operating": ("slc.10X.L0699.C01.01", "Ontario unconditional grants, all (Schedule 10)"),
    "ont_conditional": ("slc.10X.L0810.C01.01", "Ontario conditional grants (Schedule 10)"),
    "ont_capital": ("slc.10X.L0815.C01.01", "Ontario grants for tangible capital assets (Schedule 10)"),
    "can_conditional": ("slc.10X.L0820.C01.01", "Canada conditional grants (Schedule 10)"),
    "can_capital": ("slc.10X.L0825.C01.01", "Canada grants for tangible capital assets (Schedule 10)"),
    "gas_tax": ("slc.10X.L0831.C01.01", "Canada Gas Tax / Canada Community-Building Fund earned (Schedule 10)"),
    "conditional_total": ("slc.10X.L0899.C01.01", "Conditional grants subtotal (Schedule 10)"),
    "user_fees": ("slc.10X.L1299.C01.01", "User fees and service charges (Schedule 10)"),
    "expenses": ("slc.40X.L9910.C01.11", "Total expenses (Schedule 40)"),
    "aro_accretion": ("slc.74E.L9910.C01.05", "Asset retirement obligation accretion (Schedule 74E)"),
    "salaries": ("slc.40X.L9910.C01.01", "Salaries, wages and employee benefits (Schedule 40)"),
    "materials": ("slc.40X.L9910.C01.03", "Materials (Schedule 40)"),
    "contracted": ("slc.40X.L9910.C01.04", "Contracted services (Schedule 40)"),
    "external_transfers": ("slc.40X.L9910.C01.06", "External transfers (Schedule 40)"),
    "interest": ("slc.40X.L9910.C01.02", "Interest on long-term debt (Schedule 40)"),
    "amortization": ("slc.40X.L9910.C01.16", "Amortization (Schedule 40)"),
    "fn_general": ("slc.40X.L0299.C01.11", "General government (Schedule 40)"),
    "fn_protection": ("slc.40X.L0499.C01.11", "Protection services (Schedule 40)"),
    "fn_transportation": ("slc.40X.L0699.C01.11", "Transportation services (Schedule 40)"),
    "fn_environmental": ("slc.40X.L0899.C01.11", "Environmental services (Schedule 40)"),
    "fn_health": ("slc.40X.L1099.C01.11", "Health services (Schedule 40)"),
    "fn_social": ("slc.40X.L1299.C01.11", "Social and family services (Schedule 40)"),
    "fn_recreation": ("slc.40X.L1699.C01.11", "Recreation and cultural services (Schedule 40)"),
    "fn_planning": ("slc.40X.L1899.C01.11", "Planning and development (Schedule 40)"),
    "reserves": ("slc.60X.L9930.C01.03", "Reserves and discretionary reserve funds, year end (Schedule 60)"),
    "obligatory_reserves": ("slc.60X.L9930.C01.01", "Obligatory reserve funds / deferred revenue, year end (Schedule 60)"),
    "debt": ("slc.74A.L9910.C01.01", "Net long-term liabilities, year end (Schedule 74)"),
    "full_time": ("slc.80A.L0399.C01.01", "Full-time funded positions (Schedule 80)"),
    "part_time": ("slc.80A.L0399.C01.02", "Part-time funded positions (Schedule 80)"),
    "seasonal": ("slc.80A.L0399.C01.03", "Seasonal employees (Schedule 80)"),
    "res_municipal_tax": ("slc.26A.L9110.C01.04", "Residential municipal taxes levied (Schedule 26)"),
    "res_assessment": ("slc.26A.L9110.C01.16", "Residential taxable assessment, CVA (Schedule 26)"),
}


def num(v: str):
    try:
        f = float(str(v).strip())
        return int(f) if f.is_integer() else f
    except Exception:
        return None


def main() -> None:
    years: Dict[str, Dict] = {}
    for f in sorted(FIR_DIR.glob("*.csv")):
        raw = list(csv.DictReader(f.open(encoding="utf-8")))
        rows = {r["SLC"]: r["AMOUNT"] for r in raw}
        text = {r["SLC"]: (r["VALUE_TEXT"] or "").strip() for r in raw}
        y = {k: num(rows.get(slc, "")) for k, (slc, _) in LINES.items()}
        # Money that went out the door: total expenses less the accounting charge
        # for future landfill/asset retirement costs (reported from 2023).
        if y["expenses"] is not None:
            y["spent"] = y["expenses"] - (y["aro_accretion"] or 0)
        g = [y[k] or 0 for k in ("prov_operating", "conditional_total")]
        y["grants_total"] = sum(g)
        y["grants_other_provincial"] = (y["prov_operating"] or 0) - (y["ompf"] or 0) + (y["ont_conditional"] or 0) + (y["ont_capital"] or 0)
        y["grants_federal"] = (y["can_conditional"] or 0) + (y["can_capital"] or 0) + (y["gas_tax"] or 0)
        # Who signed it, who audited it, when it was filed (Schedule 02).
        y["filed"] = {"date": text.get("slc.02X.L0094.C01.02") or None,
                      "treasurer": text.get("slc.02X.L0090.C01.02") or None,
                      "auditor": text.get("slc.02X.L0091.C01.02") or None,
                      "audit_firm": text.get("slc.02X.L0092.C01.02") or None}
        years[f.stem] = {k: v for k, v in y.items() if v is not None}
    out = {
        "_about": "Township of Nipissing figures by year from the Province's Financial Information Return (FIR), "
                  "built by scripts/financial_series.py from data/financial/fir/. Each key names an FIR line (see 'lines').",
        "source": {"label": "Ontario Financial Information Return (FIR), open data",
                   "url": "https://efis.fma.csc.gov.on.ca/fir/MultiYearReport/MYCIndex.html"},
        "lines": {k: {"slc": slc, "label": lab} for k, (slc, lab) in LINES.items()},
        "years": years,
        "built": date.today().isoformat(),
    }
    OUT.write_text(json.dumps(out, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"  financial_series.json: {len(years)} years ({min(years)}–{max(years)})" if years else "  no FIR files yet")


if __name__ == "__main__":
    main()
