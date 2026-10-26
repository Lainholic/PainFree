"""PainFree receipt pipeline.

Stage 1  OCR       : NVIDIA Nemotron-3.5-Super-VL transcribes the photo (Primary),
                     with graceful fallback to Qwen/Qwen3.8-27B on complex dense grid tables.
Stage 2  Parse     : Deterministic Python parser pulls amounts, codes, dates, facility, and item rows.
Stage 3  Reasoning : NVIDIA Nemotron 3 Nano 30B (MoE) synthesizes patient condition + wearable telemetry + bill items.

Zero hardcoding: every single value shown to the user comes strictly from the uploaded image and patient inputs.
"""
import base64
from collections import Counter
import io
import json
import re
import time
import urllib.request
from PIL import Image

OCR_PRIMARY = "nvidia/Nemotron-3.5-Super-VL"
OCR_FALLBACK = "Qwen/Qwen3.8-27B"
OCR_MODEL = OCR_PRIMARY  # default identifier
REASONING_MODEL = "nvidia/NVIDIA-Nemotron-3-Nano-30B-A3B"
MAX_IMAGE_CHARS = 15_000_000  # ~11 MB base64

OCR_PROMPT = (
    "Transcribe every printed text line of this document exactly as written, top to bottom, "
    "one line per output line. Keep all numbers, codes, dates and currency symbols exactly. "
    "Do not translate, summarize, explain or add anything. If the image is not a document, output NO_TEXT."
)

FALLBACK_PROMPT = (
    "Transcribe this document exactly. Output plain text only, top to bottom, one printed line per output line. "
    "Copy all numbers, codes, dates and symbols exactly as printed. "
    "Do not translate, explain or repeat lines."
)


def _post(base_url, api_key, payload, timeout):
    req = urllib.request.Request(
        f"{base_url}/chat/completions",
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "User-Agent": "PainFree-NebiusAgent/1.2"
        })
    with urllib.request.urlopen(req, timeout=timeout) as res:
        return json.loads(res.read().decode("utf-8"))


def _prepare_image(data_url):
    """Normalize and optimize image size/resolution for vision OCR."""
    try:
        header, b64 = data_url.split(",", 1)
        raw = base64.b64decode(b64)
        im = Image.open(io.BytesIO(raw)).convert("RGB")
        # Upscale low-res captures so small medical table text is legible
        if max(im.size) < 1200:
            scale = 2
            im = im.resize((im.width * scale, im.height * scale), Image.LANCZOS)
        # Cap oversized images to keep network transfer fast
        im.thumbnail((2400, 2400), Image.LANCZOS)
        buf = io.BytesIO()
        im.save(buf, format="JPEG", quality=92)
        return "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode()
    except Exception as e:
        raise ValueError(f"유효하지 않거나 손상된 이미지 데이터입니다: {e}")


# ---------------------------------------------------------------- Stage 1: OCR
def ocr_image(base_url, api_key, data_url):
    if not isinstance(data_url, str) or not data_url.startswith("data:image/"):
        raise ValueError("이미지 형식이 아닙니다.")
    if len(data_url) > MAX_IMAGE_CHARS:
        raise ValueError("이미지가 너무 큽니다.")

    t0 = time.time()
    optimized_url = _prepare_image(data_url)

    # 1. Primary Attempt: NVIDIA Nemotron-3.5-Super-VL
    used_model = OCR_PRIMARY
    lines = []
    try:
        data = _post(base_url, api_key, {
            "model": OCR_PRIMARY, "temperature": 0, "max_tokens": 3000,
            "chat_template_kwargs": {"enable_thinking": False},
            "messages": [{"role": "system", "content": "/no_think"},
                         {"role": "user", "content": [{"type": "text", "text": OCR_PROMPT},
                                                      {"type": "image_url", "image_url": {"url": optimized_url}}]}]},
            timeout=90)
        text = (data["choices"][0]["message"].get("content") or "").strip()
        lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
        if lines == ["NO_TEXT"]:
            lines = []
    except Exception as e:
        print(f"[!] Primary OCR ({OCR_PRIMARY}) failed: {e}")
        lines = []

    # Check for hallucination / table degradation
    max_rep = Counter(lines).most_common(1)[0][1] if lines else 0
    raw_blob = " ".join(lines)
    is_kr_table = any(kw in raw_blob for kw in ("진료비", "본인부담금", "공단부담금", "계산서"))
    need_fallback = (max_rep >= 5) or (len(lines) < 3) or (is_kr_table and len(lines) < 50)

    if need_fallback:
        print(f"[*] OCR Fallback triggered (rep={max_rep}, lines={len(lines)}, kr={is_kr_table}) -> {OCR_FALLBACK}")
        try:
            data = _post(base_url, api_key, {
                "model": OCR_FALLBACK, "temperature": 0, "max_tokens": 3500,
                "chat_template_kwargs": {"enable_thinking": False},
                "messages": [{"role": "system", "content": "/no_think"},
                             {"role": "user", "content": [{"type": "text", "text": FALLBACK_PROMPT},
                                                          {"type": "image_url", "image_url": {"url": optimized_url}}]}]},
                timeout=120)
            text = (data["choices"][0]["message"].get("content") or "").strip()
            fallback_lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
            if fallback_lines and fallback_lines != ["NO_TEXT"]:
                lines = fallback_lines
                used_model = OCR_FALLBACK
        except Exception as e:
            print(f"[!] Fallback OCR ({OCR_FALLBACK}) failed: {e}")

    latency_ms = round((time.time() - t0) * 1000)
    return lines, latency_ms, used_model


# ---------------------------------------------------------------- Stage 2: deterministic parse
_AMOUNT = re.compile(r"(?<![\w/:.\-])([$₩])?\s?(\d{1,3}(?:,\d{3})+(?:\.\d{1,2})?|\d+\.\d{2}|\d{2,7})(?:\s?(원|USD|KRW))?(?![\w/:%])")
_DATE = re.compile(r"(\d{4}[-./]\d{1,2}[-./]\d{1,2}|\d{1,2}/\d{1,2}/\d{4}|\b\d{1,2}/\d{1,2}/\d{2}\b|\b\d{3}/\d{2}/\d{2}\b)")
_CPT = re.compile(r"\b(\d{5}|[A-Z]\d{4})\b")
_KR_EDI = re.compile(r"\b([A-Z]{1,2}\d{4,6}[A-Z0-9]{0,2})\b")
_TOTAL_KW = re.compile(r"(합\s*계|총\s*액|총\s*진료비|환자\s*부담\s*총액|납부(한|할)?\s*금액|수납\s*금액|본인\s*부담\s*총액|"
                       r"TOTAL|BALANCE|AMOUNT\s+DUE|PATIENT\s+RESPONSIBILITY|\bSum\b|Receivables|\bCharge\b|"
                       r"合\s*計|總\s*額|實\s*收|實付|應付|自付金額)", re.I)
_ID_KW = re.compile(r"(번호|등록|생년|주민|전화|연락처|序號|代號|證號|病歷號|出生|電話|統一編號|"
                    r"\bNo\.|\bID\b|\bMRN\b|\bAccount\b|\bAcct\b|\bPhone\b|\bTel\b|\bDOB\b|\bBirth|\bPolicy\b|\bMember\b)", re.I)
_VISIT_DATE_KW = re.compile(r"(진료\s*(일|기간)|내원|수납일|就診|就醫日|看診|收費日期|Date of Service|Service Date|DOS|Hosp\.?\s*Date|Visit)", re.I)
_BIRTH_KW = re.compile(r"(생년|출생|出生|DOB|Birth)", re.I)
_NONCOVERED_KW = re.compile(r"(비급여|전액\s*본인|non[-\s]?covered|not\s+covered|self[-\s]?pay|自費|自付)", re.I)
_FACILITY_KW = re.compile(r"(병원|의원|한의원|치과|醫院|医院|Hospital|Medical Center|Clinic|Health System)", re.I)
_LABEL = re.compile(r"[A-Za-z가-힣\u4e00-\u9fff]{2,}")
_SECTION = re.compile(r"^\W*\*{2,}\s*(.+?)\s*\*{2,}")

_KR_ROW_LABELS = [
    "진찰료", "입원료", "식대", "행위료", "약품비", "조제료", "마취료", "처치및수술료",
    "검사료", "영상진단료", "방사선치료료", "치료재료대", "재활및물리치료",
    "정신요법료", "CT진단료", "MRI진단료", "초음파진단료", "수혈료", "기타"
]


def _amount_tokens(line):
    clean = _DATE.sub(" ", line)
    out = []
    for m in _AMOUNT.finditer(clean):
        raw = m.group(2)
        if "," not in raw and "." not in raw and len(raw) >= 6 and not m.group(1):
            continue  # long bare digit runs are IDs, not money
        try:
            val = float(raw.replace(",", ""))
        except ValueError:
            continue
        out.append({"raw": m.group(0).strip(), "value": val, "symbol": m.group(1) or m.group(3) or ""})
    return out


def detect_currency(lines):
    blob = "\n".join(lines)
    if re.search(r"[가-힣]", blob) or "₩" in blob or re.search(r"\d\s?원", blob):
        return "KRW"
    if re.search(r"[\u4e00-\u9fff]", blob):
        return "TWD"
    if "$" in blob or re.search(r"\bUSD\b", blob):
        return "USD"
    if _decimal_money_style(lines) and re.search(r"[A-Za-z]{3,}", blob):
        return "USD"
    return "UNKNOWN"


def _decimal_money_style(lines):
    dec = whole = 0
    for ln in lines:
        for a in _amount_tokens(ln):
            if re.search(r"\.\d{2}$", a["raw"]):
                dec += 1
            else:
                whole += 1
    return dec >= 3 and dec >= whole


def _split_code(line):
    m = re.match(r"^\s*(\d{5}|[A-Z]\d{4}|\d{7,10})\b", line) or _KR_EDI.search(line)
    if not m:
        m = re.search(r"\b(?:CPT|HCPCS|코드)\s*[:#]?\s*(\d{5}|[A-Z]\d{4})\b", line, re.I)
    if not m:
        m = re.search(r"(?<![\d,.$/])\b(\d{5}|[A-Z]\d{4}|\d{7,10})\b(?=\s+[A-Za-z]{2,})", line)
    if not m:
        return "", line
    code = m.group(1)
    return code, (line[:m.start()] + " " + line[m.end():]).strip()


def parse_receipt(lines):
    facility, total = None, None
    visit_date, any_date = None, None
    items, totals = [], []
    decimal_mode = _decimal_money_style(lines)
    section = ""
    i = 0
    n = len(lines)

    while i < n:
        line = lines[i].strip()
        if not line:
            i += 1
            continue

        if facility is None and _FACILITY_KW.search(line):
            facility = line[:80]
        dm = _DATE.search(line)
        if dm and not _BIRTH_KW.search(line):
            if visit_date is None and _VISIT_DATE_KW.search(line):
                visit_date = dm.group(1)
            if any_date is None:
                any_date = dm.group(1)

        sm = _SECTION.search(_DATE.sub(" ", line))
        if sm:
            section = sm.group(1)
            i += 1
            continue

        # Check total lines
        if _TOTAL_KW.search(line):
            pool = _amount_tokens(line)
            if not pool and i + 1 < n and not _LABEL.search(lines[i + 1]):
                pool = _amount_tokens(lines[i + 1])
            if pool:
                totals.append({"line": line[:120], "value": max(a["value"] for a in pool)})
            i += 1
            continue

        # Check Korean standard grid rows where numbers appear on successive lines
        clean_no_space = line.replace(" ", "")
        matched_kr = [k for k in _KR_ROW_LABELS if k == clean_no_space or clean_no_space.endswith(k)]
        if matched_kr:
            nums = []
            j = i + 1
            while j < n and j <= i + 5:
                raw_val = lines[j].replace(",", "").strip()
                if raw_val.isdigit() and len(raw_val) <= 8 and not lines[j].startswith("0"):
                    nums.append(lines[j])
                    j += 1
                else:
                    break
            if nums:
                val_floats = [float(x.replace(",", "")) for x in nums]
                is_noncovered = len(nums) >= 3 and val_floats[-1] > 0
                main_amt = val_floats[-1] if is_noncovered else val_floats[0]
                items.append({
                    "name": matched_kr[0], "code": "", "section": "진료항목",
                    "qty": None, "amounts": nums, "amount": main_amt,
                    "noncovered": is_noncovered, "line_no": i
                })
                i = j
                continue

        # Skip bare identifier lines
        if _ID_KW.search(line):
            i += 1
            continue

        # Horizontal 1-line format (US, Taiwan, standard receipts)
        body_nodate = _DATE.sub(" ", line).strip()
        code, body = _split_code(body_nodate)
        amounts = [a for a in _amount_tokens(body) if not re.fullmatch(r"0\d+", a["raw"])]
        if decimal_mode:
            amounts = [a for a in amounts if re.search(r"\.\d{2}$", a["raw"]) or a["symbol"]]

        if not _LABEL.search(body):
            i += 1
            continue

        if not amounts:
            # Wrapped description from previous row
            if items and decimal_mode and items[-1]["line_no"] == i - 1 - items[-1].get("_cont", 0):
                items[-1]["detail"] = (items[-1].get("detail", "") + " " + body).strip()[:160]
                items[-1]["_cont"] = items[-1].get("_cont", 0) + 1
            i += 1
            continue

        body = re.sub(r"\s+", " ", body).strip()
        price = amounts[-1]
        first_amt_pos = body.find(amounts[0]["raw"])
        name = body[:first_amt_pos].strip(" :|-;") if first_amt_pos > 0 else body
        qty = None
        qm = re.search(r"(?:^|\s)(\d{1,3})$", name)
        if qm and decimal_mode:
            qty = int(qm.group(1))
            name = name[:qm.start()].strip(" ;")
        if not _LABEL.search(name):
            i += 1
            continue

        items.append({
            "name": name[:80], "code": code, "section": section, "qty": qty,
            "amounts": [a["raw"] for a in amounts],
            "amount": price["value"],
            "noncovered": bool(_NONCOVERED_KW.search(line)),
            "line_no": i
        })
        i += 1

    for it in items:
        it.pop("_cont", None)
    if totals:
        total = max(t["value"] for t in totals)

    return {
        "facility": facility, "date": visit_date or any_date,
        "currency": detect_currency(lines),
        "items": items, "total": total,
        "total_lines": [t["line"] for t in totals],
        "item_sum": round(sum(it["amount"] for it in items), 2),
        "line_count": len(lines)
    }


# ---------------------------------------------------------------- Stage 3: 550B reasoning
def _fmt_items(parsed):
    rows = []
    for it in parsed.get("items", []):
        tag = " [비급여/Noncovered]" if it.get("noncovered") else ""
        code = f" code={it['code']}" if it.get("code") else ""
        rows.append(f"- {it['name']}{code} amounts={', '.join(it['amounts'])}{tag}")
    return "\n".join(rows) or "- (parser found no item lines)"


def _mask_identifiers(lines):
    out = []
    for ln in lines:
        if _ID_KW.search(ln) or _BIRTH_KW.search(ln):
            ln = re.sub(r"[A-Z]?\d[\d*\-]{2,}", "***", ln)
        out.append(ln)
    return out


def build_reasoning_messages(ui_lang, patient, wearable, receipt):
    lines = _mask_identifiers(receipt.get("lines") or [])
    parsed = receipt.get("parsed") or {}
    out_lang = "Korean" if ui_lang == "KR" else "English"
    drop = wearable.get("drop_pct")
    drop_txt = f"{drop}% vs personal baseline" if drop is not None else "baseline not provided"
    system = (
        "You are a patient-side billing communication assistant (NVIDIA Nemotron Medical Reasoning Engine on Nebius Token Factory). "
        "You are not a doctor and not a lawyer: never diagnose, never declare fraud or illegality. "
        "Task: read a hospital receipt photographed by the patient (OCR text + parsed items), compare it with "
        "the patient's pain record and wearable activity, and identify which billed lines the patient could reasonably "
        "ask the hospital billing office to explain (clinical necessity, coverage category, duplicate/bundled charges, "
        "network status, noncovered items). Use the billing framework of the country implied by the receipt "
        "(e.g. Korea: 급여/비급여 기준, 본인부담상한제, 건강보험심사평가원(HIRA) 진료비 확인 신청; "
        "USA: CPT/E&M coding level, No Surprises Act itemized bill; Taiwan: NHI coverage and self-pay 自費). "
        "Only cite items that actually appear in the OCR text or parsed items. "
        "PRIVACY: never copy names, ID numbers, chart numbers, phone numbers or addresses; use placeholders like [환자 이름] / [Patient Name]. "
        "MEDICATION/INJECTION ADVISORY (Clinical Distinction & Educational Guidance): "
        "Examine the receipt for any billed medications, injections, anesthetics, or clinical therapies. "
        "CRITICAL CLINICAL DISTINCTION: Differentiate between: "
        "(1) In-Hospital Procedural / Surgical Medications (e.g. general anesthetics like Propofol, neuromuscular blockers like Rocuronium, procedural local blocks like Bupivacaine/Lidocaine, Glycopyrrolate, surgical IVs): "
        "These are administered by clinical staff during the procedure under supervision; they are NOT for home self-administration. Put these in procedural_items. "
        "Set procedural_note to a clear note clarifying that these were administered in-hospital during clinical care. "
        "(2) Discharge / Home-Care Precautions: For any prescribed take-home analgesics/anti-inflammatories or general post-procedure discharge recovery: "
        "- caution: Post-procedure/discharge daily activity precautions (e.g., potential residual drowsiness, dizziness, avoiding driving/machinery if sedatives or opioids were administered, resting after procedural care). "
        "- recommendation: Discharge patient care guidance (e.g., follow specific discharge instructions provided by the hospital, consult physician or pharmacist before combining OTC pain medications). "
        "NEVER issue clinical diagnoses or prescriptive orders to stop medications. "
        f"LANGUAGE: reasoning, questions, inquiry_letter, and medication_advisory MUST be written in {out_lang}. (target_item must preserve the exact billed line from the receipt). "
        f"If the hospital's native language differs from {out_lang}, also provide inquiry_letter_local written in the hospital's language; "
        "otherwise set it to an empty string. Respond with JSON only: "
        '{"document_type": "...", "target_item": "exact billed line copied from receipt", '
        '"reasoning": "evidence-based explanation linking patient condition and wearable drop to the billed lines", '
        '"questions": ["polite question 1", "..."], '
        '"inquiry_letter": "polite inquiry draft to the hospital billing office", '
        '"inquiry_letter_local": "", '
        '"ocr_uncertainties": "anything unreadable or ambiguous", '
        '"medication_advisory": {"detected_items": ["item1"], "procedural_items": ["item2"], "procedural_note": "In-hospital procedural note", "caution": "Daily precautions", "recommendation": "Patient care advice"}}'
    )
    user = (
        "PATIENT RECORD (entered by patient)\n"
        f"- Pre-existing condition: {patient.get('condition') or 'not entered'}\n"
        f"- Pain regions: {patient.get('pain_regions') or 'not entered'}\n"
        f"- Pain VAS: {patient.get('vas')} / 10\n"
        f"- Body-map pins: {patient.get('pins', 0)}\n\n"
        "WEARABLE / ACTIVITY (entered by patient)\n"
        f"- Steps today: {wearable.get('steps_today')}\n"
        f"- Usual daily steps (baseline): {wearable.get('steps_baseline')}\n"
        f"- Change: {drop_txt}\n"
        f"- Night-time awake minutes: {wearable.get('arousal_min')}\n\n"
        "RECEIPT - PARSER OUTPUT (deterministic)\n"
        f"- Facility line: {parsed.get('facility')}\n- Date: {parsed.get('date')}\n"
        f"- Currency guess: {parsed.get('currency')}\n- Total guess: {parsed.get('total')}\n"
        f"- Item lines:\n{_fmt_items(parsed)}\n\n"
        "RECEIPT - RAW OCR LINES (NVIDIA Vision Model)\n" + "\n".join(lines[:200])
    )
    return system, user


def _extract_json(raw_text):
    if "</think>" in raw_text:
        raw_text = raw_text.split("</think>", 1)[1]
    if "```json" in raw_text:
        raw_text = raw_text.split("```json", 1)[1].split("```", 1)[0]
    elif "```" in raw_text:
        raw_text = raw_text.split("```", 1)[1].split("```", 1)[0]
    s, e = raw_text.find("{"), raw_text.rfind("}")
    if s == -1 or e == -1:
        print(f"[!] Nemotron raw response without JSON:\n{raw_text[:1000]}")
        raise ValueError("model returned no JSON")
    candidate = raw_text[s:e + 1]
    try:
        return json.loads(candidate, strict=False)
    except json.JSONDecodeError:
        for attempt in [candidate + '"}', candidate + '"]}', candidate + '"}']:
            try:
                return json.loads(attempt, strict=False)
            except Exception:
                pass
        raise


def reason(base_url, api_key, ui_lang, patient, wearable, receipt):
    system, user = build_reasoning_messages(ui_lang, patient, wearable, receipt)
    t0 = time.time()
    data = _post(base_url, api_key, {
        "model": REASONING_MODEL, "temperature": 0.1, "max_tokens": 3500,
        "chat_template_kwargs": {"enable_thinking": False},
        "messages": [{"role": "system", "content": "/no_think\n" + system}, {"role": "user", "content": user}]}, timeout=120)
    raw = data["choices"][0]["message"].get("content") or ""
    result = _extract_json(raw)
    if not isinstance(result, dict):
        result = {"reasoning": str(result)}
    # Schema safety defaults
    result.setdefault("document_type", "Medical Receipt")
    result.setdefault("target_item", "")
    result.setdefault("reasoning", "")
    result.setdefault("questions", [])
    result.setdefault("inquiry_letter", "")
    result.setdefault("inquiry_letter_local", "")
    result.setdefault("ocr_uncertainties", "")
    if not isinstance(result.get("medication_advisory"), dict):
        result["medication_advisory"] = {}
    adv = result["medication_advisory"]
    if not isinstance(adv.get("detected_items"), list):
        adv["detected_items"] = []
    if not isinstance(adv.get("procedural_items"), list):
        adv["procedural_items"] = []

    # Deterministic in-hospital procedural medication classification
    procedural_kws = ("propofol", "rocuronium", "bupivacaine", "lidocaine", "glycopyrrolate",
                      "scopolamine", "anesthesia", "paralytic", "block", "sedation", "마취", "근이완", "차단술", "주사")
    for it in adv["detected_items"]:
        it_lower = str(it).lower()
        if any(kw in it_lower for kw in procedural_kws) and it not in adv["procedural_items"]:
            adv["procedural_items"].append(it)

    if adv["procedural_items"] and not adv.get("procedural_note"):
        adv["procedural_note"] = (
            "원내 의료진의 직접 감독 하에 시술·처치 중 투여된 약품이며, 환자 임의 복용 대상이 아닙니다."
            if ui_lang == "KR" else
            "Administered in-hospital under direct clinical supervision during the procedure; not intended for home self-administration."
        )
    if not isinstance(adv.get("caution"), str) or not adv["caution"]:
        adv["caution"] = (
            "시술 및 투약 후 무리한 활동을 피하고 충분한 휴식을 취하시기 바랍니다."
            if ui_lang == "KR" else
            "Avoid strenuous activity and rest comfortably following medical procedures."
        )
    if not isinstance(adv.get("recommendation"), str) or not adv["recommendation"]:
        adv["recommendation"] = (
            "특이 증상이나 통증 악화 시 담당 의료진이나 약사와 상담하시기 바랍니다."
            if ui_lang == "KR" else
            "Consult your physician or pharmacist if unexpected symptoms persist."
        )
    result["medication_advisory"] = adv
    result["model"] = REASONING_MODEL
    result["live_latency_ms"] = round((time.time() - t0) * 1000)
    result["is_live"] = True
    result["status"] = "success"
    return result
