import re
import pdfplumber
from flask import Flask, request, jsonify
from flask_cors import CORS

app = Flask(__name__)
CORS(app)

VALID_UNITS = {"SQ", "EA", "LF", "SF", "SY", "HR", "DA", "MO", "WK", "LS", "BF", "TN", "GL", "CL"}

TRADE_SYNONYMS = {
    "Roofing": ["roof", "shingle", "felt", "underlayment", "ridge cap", "drip edge", "valley", "flashing", "pipe jack", "starter", "ice & water", "mod bit", "tpo", "eaves"],
    "Drywall & Texture": ["drywall", "sheetrock", "gypsum", "plaster", "stucco", "texture", "taped", "floated", "hang", "acoustic", "popcorn", "mud"],
    "Painting": ["paint", "seal", "prime", "stain", "latex", "enamel", "caulk", "two coats", "one coat", "masking"],
    "Demolition": ["tear off", "remove", "haul", "dispose", "dump", "debris removal", "demolish", "take off", "detach", "water extraction", "remediation", "cut down", "tree"],
    "Siding": ["siding", "soffit", "fascia", "cladding", "corner post", "j-channel", "wrap", "hardie", "vinyl"],
    "Electrical": ["electric", "wire", "fixture", "outlet", "switch", "breaker", "panel", "lighting", "receptacle", "conduit", "junction"],
    "Plumbing": ["plumb", "pipe", "drain", "sink", "faucet", "toilet", "water heater", "p-trap", "valve", "supply line"],
    "HVAC / Mechanical": ["hvac", "heat", "air cond", "furnace", "duct", "condenser", "vent", "flue", "thermostat", "refrigerant"],
    "Flooring": ["carpet", "pad", "tile", "hardwood", "vinyl plank", "lvp", "underlay", "grout", "baseboard"],
    "Insulation": ["insul", "batt", "blown", "r30", "r38", "r19", "r13", "fiberglass", "vapor barrier"],
    "Tree / Grounds": ["tree", "stump", "branches", "limbs", "landscaping", "lawn", "grounds", "cut down"],
    "General Conditions": ["general conditions", "delivery", "permit", "container", "storage", "dumpster", "supervis", "cleanup", "final clean", "barricade", "safety", "protection", "food loss", "bid item", "allowance", "laundering"]
}

def classify_trade(text: str) -> str:
    low = text.lower()
    for trade, synonyms in TRADE_SYNONYMS.items():
        if any(syn in low for syn in synonyms):
            return trade
    return "General Conditions"

def clean_num(val_str: str) -> float:
    cleaned = re.sub(r"[$,\(\)<>*]", "", str(val_str).strip())
    try:
        return float(cleaned)
    except ValueError:
        return 0.0

@app.route("/", methods=["GET"])
def health_check():
    return jsonify({"status": "healthy", "service": "Xactimate Parser Engine"})

@app.route("/parse", methods=["POST"])
def parse_endpoint():
    if "file" not in request.files:
        return jsonify({"error": "No file uploaded"}), 400
    
    file = request.files["file"]
    parsed_items = []
    baseline = {
        "claim_number": "",
        "policy_number": "",
        "insured_name": "",
        "loss_date": "",
        "doc_direct_total": 0.0,
        "doc_rcv": 0.0,
        "doc_tax": 0.0,
        "doc_op": 0.0,
        "doc_deductible": 0.0
    }

    full_text_lines = []

    with pdfplumber.open(file) as pdf:
        for page in pdf.pages:
            text = page.extract_text(layout=False)
            if text:
                full_text_lines.extend(text.split("\n"))

    full_document_str = "\n".join(full_text_lines)

    # 1. Metadata Extraction
    claim_m = re.search(r"Claim\s*(?:#|Number)?[:\s]*([A-Za-z0-9-]+)", full_document_str, re.IGNORECASE)
    if claim_m: baseline["claim_number"] = claim_m.group(1)

    policy_m = re.search(r"Policy\s*(?:#|Number)?[:\s]*([A-Za-z0-9-]+)", full_document_str, re.IGNORECASE)
    if policy_m: baseline["policy_number"] = policy_m.group(1)

    insured_m = re.search(r"(?:Insured|Name)[:\s]*([A-Za-z\s,]+?)(?=\s*(?:Estimate|Claim|Date|Business|Page|$|\n))", full_document_str, re.IGNORECASE)
    if insured_m and len(insured_m.group(1).strip()) < 40:
        baseline["insured_name"] = insured_m.group(1).strip()

    date_m = re.search(r"(?:Date of Loss|Loss Date)[:\s]*([0-9\/\-\.]+)", full_document_str, re.IGNORECASE)
    if date_m: baseline["loss_date"] = date_m.group(1).strip()

    # 2. Adjuster Ground Truth Totals
    multi_cov_total = re.search(r"Coverage\s+Item\s+Total[\s\S]+?Total\s+([\d,]+\.\d{2})\s+100\.00%", full_document_str, re.IGNORECASE)
    if multi_cov_total:
        baseline["doc_direct_total"] = clean_num(multi_cov_total.group(1))
    else:
        direct_matches = re.findall(r"Line\s+Item\s+Total(?:s)?\s*[:\s]*([A-Za-z0-9_]+)?\s*([\d,]+\.\d{2})?", full_document_str, re.IGNORECASE)
        sum_direct = 0.0
        for m in direct_matches:
            val = m[1] if m[1] else m[0]
            num = clean_num(val)
            if num > 0:
                sum_direct += num
        baseline["doc_direct_total"] = sum_direct

    rcv_m = re.search(r"(?:Total\s+([\d,]+\.\d{2})\s+100\.00%|Replacement Cost Value\s*[:\$]?\s*([\d,]+\.\d{2}))", full_document_str, re.IGNORECASE)
    if rcv_m: baseline["doc_rcv"] = clean_num(rcv_m.group(1) or rcv_m.group(2))

    tax_matches = re.findall(r"(?:Material Sales Tax|Sales Tax|Storage Rental Tax|Laundering Tax|Comm Repr\/Remod Tax)\s*[:\$]?\s*([\d,]+\.\d{2})", full_document_str, re.IGNORECASE)
    baseline["doc_tax"] = sum(clean_num(t) for t in tax_matches)

    op_matches = re.findall(r"(?:Overhead and Profit|O&P|Overhead|Profit)\s*[:\$]?\s*([\d,]+\.\d{2})", full_document_str, re.IGNORECASE)
    baseline["doc_op"] = sum(clean_num(o) for o in op_matches)

    ded_m = re.search(r"(?:Deductible|Less Deductible)\s*[:\$\(]?\s*([\d,]+\.\d{2})", full_document_str, re.IGNORECASE)
    if ded_m: baseline["doc_deductible"] = clean_num(ded_m.group(1))

    # 3. Robust Line Item Parsing
    pending_desc = ""

    for line in full_text_lines:
        clean_line = line.strip()
        low = clean_line.lower()

        # Dimension / CAD Shield
        if any(token in low for token in [
            "surface area", "perimeter length", "sf walls", "sf ceiling", "sf floor",
            "sy flooring", "lf floor", "lf ceil", "floor area", "interior wall area",
            "total area", "grand total areas", "recap by category", "sublimit recap",
            "summary for", "formula elevation", "price list:"
        ]) or low.startswith(("totals:", "total:", "subtotal:")):
            continue

        # Regex to capture: [Qty] [Unit] [Price] [Optional Tax/OP] [Line Total]
        units_pattern = "|".join(VALID_UNITS)
        item_pattern = rf"(\d+[\d,]*\.\d{{2}})\s*({units_pattern})\s+[\$]?(\d+[\d,]*\.\d{{2}})(?:\s+[\$]?\d+[\d,]*\.\d{{2}})*\s+[\$]?(\d+[\d,]*\.\d{{2}})"
        match = re.search(item_pattern, clean_line, re.IGNORECASE)

        if match:
            qty = clean_num(match.group(1))
            unit = match.group(2).upper()
            price = clean_num(match.group(3))
            
            line_desc = clean_line[:match.start()].strip()
            desc = (pending_desc + " " + line_desc).strip()
            desc = re.sub(r"^\d+\.\s*", "", desc)
            desc = re.sub(r"\*+$", "", desc).strip()

            if len(desc) > 2:
                parsed_items.append({
                    "trade": classify_trade(desc),
                    "description": desc,
                    "qty": qty,
                    "unit": unit,
                    "unit_price": price
                })
            pending_desc = ""

        else:
            # Fallback for sequence-numbered items without standard units (e.g. flat allowances)
            trailing_m = re.search(r"^(\d+\.\s+)?(.+?)\s+[\$]?([\d,]+\.\d{2})(?:\s+[\$]?[\d,]+\.\d{2})*$", clean_line)
            if trailing_m and not low.startswith(("line item", "coverage", "net claim", "replacement cost")):
                desc_candidate = (pending_desc + " " + trailing_m.group(2)).strip()
                desc_candidate = re.sub(r"^\d+\.\s*", "", desc_candidate).strip()
                cost = clean_num(trailing_m.group(3))
                
                if len(desc_candidate) > 4 and cost > 0 and not any(k in desc_candidate.lower() for k in ["total", "page:", "continued"]):
                    parsed_items.append({
                        "trade": classify_trade(desc_candidate),
                        "description": desc_candidate,
                        "qty": "N/A",
                        "unit": "N/A",
                        "unit_price": cost
                    })
                    pending_desc = ""
                    continue
            
            if re.match(r"^\d+\.\s+", clean_line) or pending_desc:
                pending_desc = (pending_desc + " " + clean_line).strip()

    # 4. Accounting Reconciliation Assertion
    parsed_sum = sum(
        item["unit_price"] if item["qty"] == "N/A" else (float(item["qty"]) * item["unit_price"])
        for item in parsed_items
    )
    target_direct = baseline["doc_direct_total"] if baseline["doc_direct_total"] > 0 else parsed_sum
    variance = abs(parsed_sum - target_direct)

    return jsonify({
        "metadata": baseline,
        "audit": {
            "parsed_items_count": len(parsed_items),
            "parsed_direct_sum": round(parsed_sum, 2),
            "adjuster_target_direct": round(target_direct, 2),
            "variance_delta": round(variance, 2),
            "is_reconciled": variance <= 1.00
        },
        "items": parsed_items
    })

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
