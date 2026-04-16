#!/usr/bin/env python3
"""
Nipissing Township By-Law Scraper — v2

Strategy: Minutes-first approach.
  1. Scrape the by-laws listing page for already-published by-laws
  2. Scrape ALL council meeting minutes (machine-readable PDFs) to find every
     by-law that was passed, with vote records (Moved by / Seconded by)
  3. For agenda packages (scanned image PDFs), use OCR if available to find
     additional by-law details and extract standalone by-law PDFs
  4. Generate AI summaries via Anthropic API

Dependencies:
  pip install requests beautifulsoup4 pymupdf anthropic
  Optional for OCR: pip install pytesseract pillow
                     + system package: tesseract-ocr
"""

import json
import os
import re
import sys
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

try:
    import fitz  # PyMuPDF
except ImportError:
    print("ERROR: PyMuPDF not installed. Run: pip install pymupdf")
    sys.exit(1)

# Optional: OCR for scanned PDFs
try:
    import pytesseract
    from PIL import Image
    OCR_AVAILABLE = True
except ImportError:
    OCR_AVAILABLE = False

# Optional: AI summaries
try:
    import anthropic
    ANTHROPIC_AVAILABLE = True
except ImportError:
    ANTHROPIC_AVAILABLE = False


# ── Config ──────────────────────────────────────────────
BASE_URL = "https://nipissingtownship.com"
BYLAWS_PAGE = f"{BASE_URL}/municipal-information/by-laws/"
COUNCIL_PAGE = f"{BASE_URL}/council-meeting-dates-agendas-minutes/"
DATA_FILE = Path("docs/bylaws-data.json")
RES_FILE = Path("docs/resolutions-data.json")
PDF_DIR = Path("bylaws")
HEADERS = {"User-Agent": "NipissingBylawArchiver/2.0 (civic transparency project)"}


# ── Utilities ───────────────────────────────────────────

def load_data():
    if DATA_FILE.exists():
        with open(DATA_FILE) as f:
            return json.load(f)
    return {"last_updated": None, "source": BYLAWS_PAGE, "bylaws": []}


def save_data(data):
    data["last_updated"] = datetime.now().strftime("%Y-%m-%d")
    DATA_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(DATA_FILE, "w") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    print(f"  Saved {len(data['bylaws'])} by-laws to {DATA_FILE}")


def load_resolutions():
    if RES_FILE.exists():
        with open(RES_FILE) as f:
            return json.load(f)
    return {"last_updated": None, "resolutions": []}


def fetch_page(url):
    print(f"  GET {url}")
    r = requests.get(url, headers=HEADERS, timeout=30)
    r.raise_for_status()
    return BeautifulSoup(r.text, "html.parser")


def download_pdf(url, dest_dir):
    dest_dir = Path(dest_dir)
    dest_dir.mkdir(parents=True, exist_ok=True)
    filename = re.sub(r'[^\w\-\.]', '_', url.split("/")[-1])
    path = dest_dir / filename
    if path.exists():
        return path
    try:
        print(f"    ↓ {filename}")
        r = requests.get(url, headers=HEADERS, timeout=120)
        r.raise_for_status()
        with open(path, "wb") as f:
            f.write(r.content)
        return path
    except Exception as e:
        print(f"    WARN: download failed {filename}: {e}")
        return None


def extract_pdf_text(pdf_path):
    """Extract text. Tries embedded text first, then OCR."""
    try:
        doc = fitz.open(str(pdf_path))
        text = ""
        for page in doc:
            text += page.get_text() + "\n"
        doc.close()
        if len(text.strip()) > 100:
            return text
        # Fallback to OCR
        if OCR_AVAILABLE:
            return ocr_pdf(pdf_path)
        return text
    except Exception as e:
        print(f"    WARN: PDF read failed {pdf_path}: {e}")
        return ""


def ocr_pdf(pdf_path):
    """OCR a scanned PDF page-by-page."""
    try:
        doc = fitz.open(str(pdf_path))
        out = ""
        for page in doc:
            mat = fitz.Matrix(300 / 72, 300 / 72)
            pix = page.get_pixmap(matrix=mat)
            img = Image.frombytes("RGB", [pix.width, pix.height], pix.samples)
            out += pytesseract.image_to_string(img) + "\n"
        doc.close()
        return out
    except Exception as e:
        print(f"    WARN: OCR failed {pdf_path}: {e}")
        return ""


def parse_year(number):
    """Get year from by-law number like '2024-33'. None for legacy."""
    m = re.match(r'(\d{4})[\-–]\d', number)
    return int(m.group(1)) if m else None


# ── Step 1: Scrape the By-Laws listing page ────────────

def scrape_bylaws_page():
    print("\n═══ Step 1: By-Laws Page ═══")
    soup = fetch_page(BYLAWS_PAGE)
    bylaws = []
    content = soup.find("div", class_="entry-content") or soup.find("article") or soup

    for link in content.find_all("a", href=True):
        href = link["href"]
        text = link.get_text(strip=True)
        if not text:
            continue

        # Match "YYYY-NN Title" or "NNNN Title" (legacy)
        m = re.match(r'^(\d{4}[\-–]\d{1,3})\s+(.+)', text)
        if not m:
            m = re.match(r'^(\d{3,4})\s+(.+)', text)
        if not m:
            continue

        number = m.group(1).replace("–", "-")
        title = re.sub(r'\s+', ' ', m.group(2)).strip()
        if title.startswith("- "):
            title = title[2:]

        year = parse_year(number)
        # For legacy numbers, try to get year from URL
        if year is None:
            url_m = re.search(r'/(\d{4})[-–]', href)
            if url_m:
                y = int(url_m.group(1))
                if 1990 <= y <= 2030:
                    year = y

        pdf_url = urljoin(BASE_URL, href) if href.endswith(('.pdf', '.docx')) else None
        page_url = urljoin(BASE_URL, href) if not pdf_url else None

        bylaws.append({
            "number": number,
            "year": year,
            "title": title,
            "date_passed": None,
            "pdf_url": pdf_url,
            "page_url": page_url,
            "source": "bylaws_page",
            "status": "approved",
            "votes": None,
            "meeting_date": None,
            "agenda_package_url": None,
            "minutes_url": None,
        })

    print(f"  Found {len(bylaws)} by-laws on listing page")
    return bylaws


# ── Step 2: Scrape council meeting links ───────────────

ARCHIVE_BASE = "https://nipissing.ca"
ARCHIVE_YEARS = list(range(2018, 2027))  # 2018 through 2026

def scrape_council_meetings():
    """Scrape meeting URLs from council.chriswjohnston.ca (the archive mirror).
    
    This is far more reliable than the township site because:
    - All years 2018-2026 are indexed with direct PDF links
    - Consistent table format, easy to parse
    - No URL probing needed
    
    Falls back to the township site for any years not in the archive.
    """
    print("\n═══ Step 2: Council Meeting Archive ═══")
    meetings = []
    
    for year in ARCHIVE_YEARS:
        url = f"{ARCHIVE_BASE}/{year}/"
        try:
            soup = fetch_page(url)
        except Exception as e:
            print(f"  WARN: Could not fetch {year} archive: {e}")
            continue
        
        # Find all table rows
        rows = soup.select("table tr")
        for row in rows:
            cells = row.find_all("td")
            if not cells:
                continue
            
            # First cell has the meeting date
            date_cell = cells[0].get_text(strip=True)
            # Clean up "Special" badges
            date_text = re.sub(r'Special$', '', date_cell).strip()
            dm = re.search(r'([A-Z][a-z]+\s+\d{1,2},?\s+\d{4})', date_text)
            if not dm:
                continue
            try:
                d = datetime.strptime(dm.group(1).replace(",", ""), "%B %d %Y")
            except ValueError:
                continue
            
            is_special = "special" in date_cell.lower()
            
            # Find all links in the row
            all_links = row.find_all("a", href=True)
            agenda, minutes, package = None, None, None
            minutes_list = []  # Some rows have multiple minutes (special + regular)
            
            year_base = f"{ARCHIVE_BASE}/{year}/"
            
            for a in all_links:
                href = a["href"]
                text = a.get_text(strip=True).lower()
                full_url = href if href.startswith("http") else urljoin(year_base, href)
                
                if "agenda package" in text or "agenda-package" in href.lower() or \
                   "council-package" in href.lower() or "council-agenda-package" in href.lower() or \
                   ("package" in text and "agenda" in text):
                    package = full_url
                elif "minutes" in text and href.endswith(".pdf"):
                    minutes_list.append(full_url)
                elif "agenda" in text and href.endswith(".pdf") and "package" not in text:
                    agenda = full_url
            
            # Use first minutes PDF (or None)
            minutes = minutes_list[0] if minutes_list else None
            
            meetings.append({
                "date": d.strftime("%Y-%m-%d"),
                "date_display": d.strftime("%B %d, %Y"),
                "is_special": is_special,
                "agenda_url": agenda,
                "minutes_url": minutes,
                "package_url": package,
                "year": d.year,
            })
            
            # If there are multiple minutes (e.g., special + regular on same day),
            # add extra entries for the additional minutes
            for extra_minutes in minutes_list[1:]:
                meetings.append({
                    "date": d.strftime("%Y-%m-%d") + "-special",
                    "date_display": d.strftime("%B %d, %Y") + " (Special)",
                    "is_special": True,
                    "agenda_url": None,
                    "minutes_url": extra_minutes,
                    "package_url": None,
                    "year": d.year,
                })
        
        year_count = sum(1 for m in meetings if m.get("year") == year)
        if year_count:
            print(f"  {year}: {year_count} meetings")

    # Also try the township main page for any meetings not yet in the archive
    try:
        soup = fetch_page(COUNCIL_PAGE)
        content = soup.find("div", class_="entry-content") or soup.find("article") or soup
        existing_dates = {m["date"] for m in meetings}
        extra = 0
        
        for line in str(content).split("<br"):
            ls = BeautifulSoup(line, "html.parser")
            lt = ls.get_text()
            dm = re.search(r'([A-Z][a-z]+\s+\d{1,2},?\s+\d{4})', lt, re.IGNORECASE)
            if not dm:
                continue
            try:
                d = datetime.strptime(dm.group(1).replace(",", ""), "%B %d %Y")
            except ValueError:
                continue
            
            ds = d.strftime("%Y-%m-%d")
            if ds in existing_dates:
                continue
            
            links = ls.find_all("a", href=True)
            agenda, minutes, package = None, None, None
            for a in links:
                h = a["href"]
                t = a.get_text(strip=True).lower()
                full = urljoin(BASE_URL, h)
                if "agenda package" in t or "agenda-package" in h.lower() or "council-package" in h.lower():
                    package = full
                elif "minutes" in t:
                    minutes = full
                elif "agenda" in t:
                    agenda = full
            
            if minutes:  # Only add if there are actual minutes
                meetings.append({
                    "date": ds,
                    "date_display": d.strftime("%B %d, %Y"),
                    "is_special": "special" in lt.lower(),
                    "agenda_url": agenda,
                    "minutes_url": minutes,
                    "package_url": package,
                    "year": d.year,
                })
                extra += 1
        
        if extra:
            print(f"  Township site: {extra} additional meetings")
    except Exception as e:
        print(f"  WARN: Township page fallback failed: {e}")

    meetings.sort(key=lambda m: m["date"])
    print(f"\n  Total meetings: {len(meetings)}")
    print(f"    with minutes: {sum(1 for m in meetings if m['minutes_url'])}")
    print(f"    with packages: {sum(1 for m in meetings if m['package_url'])}")
    return meetings


# ── Step 3: Parse minutes for by-laws ──────────────────

def parse_bylaws_from_minutes(text, meeting):
    """Extract every by-law passed from meeting minutes text.

    Nipissing minutes follow this pattern:
      RYYYY-NNN Mover, Seconder:
      That we pass By-Law Number YYYY-NN, being a By-Law to <title>.
      Read a first, second and third time and passed ... Carried.
    """
    results = []

    # Split text into resolution blocks
    blocks = re.split(r'(R\d{4}-\d+)', text)
    for i in range(1, len(blocks), 2):
        res = blocks[i]
        body = blocks[i + 1] if i + 1 < len(blocks) else ""

        # Does this resolution pass a by-law?
        bm = re.search(
            r'(?:pass|adopt)\s+By[\-\s]?Law\s*(?:No\.?\s*|Number\s*)?(\d{4}[\-–]\d{1,3})',
            body, re.IGNORECASE
        )
        if not bm:
            continue

        bylaw_num = bm.group(1).replace("–", "-")

        # Mover & seconder (first line of body)
        mm = re.match(r'\s*([A-Z]\.\s*\w+)\s*,\s*([A-Z]\.\s*\w+)', body)
        mover = mm.group(1).strip() if mm else None
        seconder = mm.group(2).strip() if mm else None

        # Title: "being a By-Law to <title>"
        tm = re.search(
            r'being\s+a\s+By[\-\s]?Law\s+(?:to\s+)?(.+?)(?:\.\s*$|\.\s*Read\s|;\s*Read\s)',
            body, re.IGNORECASE | re.DOTALL
        )
        title = None
        if tm:
            title = re.sub(r'\s+', ' ', tm.group(1)).strip()
            if len(title) > 150:
                title = title[:147] + "..."

        status = "approved" if re.search(r'\bCarried\b', body) else "pending"
        votes = f"Moved by {mover}, Seconded by {seconder}" if mover and seconder else None

        results.append({
            "number": bylaw_num,
            "year": parse_year(bylaw_num),
            "title": title or f"By-Law {bylaw_num}",
            "date_passed": meeting["date"],
            "pdf_url": None,
            "page_url": None,
            "source": "minutes",
            "status": status,
            "votes": votes,
            "meeting_date": meeting["date"],
            "agenda_package_url": meeting.get("package_url"),
            "minutes_url": meeting.get("minutes_url"),
        })

    return results


def scrape_all_minutes(meetings):
    print("\n═══ Step 3: Parsing Minutes for By-Laws ═══")
    all_bylaws = []

    for meeting in meetings:
        if not meeting.get("minutes_url"):
            continue
        if meeting.get("date") == "special-meetings":
            continue  # Handle separately

        minutes_type = meeting.get("minutes_type", "pdf")
        text = None

        if minutes_type == "html":
            # WordPress sub-page — fetch HTML and extract text
            try:
                soup = fetch_page(meeting["minutes_url"])
                content = soup.find("div", class_="entry-content") or soup.find("article")
                if content:
                    text = content.get_text(separator="\n", strip=True)
            except Exception as e:
                print(f"    WARN: Failed to fetch HTML minutes for {meeting['date']}: {e}")
                continue
        else:
            # PDF minutes (2024+)
            pdf_path = download_pdf(meeting["minutes_url"], "temp_pdfs/minutes")
            if not pdf_path:
                continue
            text = extract_pdf_text(pdf_path)

        if not text or len(text.strip()) < 100:
            print(f"    WARN: No text from {meeting['date']} minutes")
            continue

        found = parse_bylaws_from_minutes(text, meeting)
        if found:
            labels = ", ".join(f"{b['number']}" for b in found)
            print(f"    {meeting['date_display']}: {len(found)} by-law(s) — {labels}")
        all_bylaws.extend(found)

    # Also parse the special-meetings page if present
    special = [m for m in meetings if m.get("date") == "special-meetings"]
    if special:
        m = special[0]
        try:
            soup = fetch_page(m["minutes_url"])
            content = soup.find("div", class_="entry-content") or soup.find("article")
            if content:
                text = content.get_text(separator="\n", strip=True)
                # This page may contain multiple meetings; parse them all
                # The resolution parser will handle it since it splits on R####-###
                found = parse_bylaws_from_minutes(text, {
                    **m,
                    "date": "special",
                    "date_display": "Special Meeting",
                })
                if found:
                    print(f"    Special Meetings page: {len(found)} by-law(s)")
                all_bylaws.extend(found)
        except Exception as e:
            print(f"    WARN: Special meetings page failed: {e}")

    print(f"\n  Total by-laws from minutes: {len(all_bylaws)}")
    return all_bylaws


# ── Step 4: Extract by-laws from agenda packages ─────

def scrape_agenda_packages(meetings, known_numbers):
    """Extract by-law PDFs from agenda packages.
    Downloads each package, finds by-law pages via text extraction
    (falling back to OCR if available), and saves standalone PDFs.
    
    Also tries the council archive mirror at council.chriswjohnston.ca
    if the township URL fails."""
    print("\n═══ Step 4: Scanning Agenda Packages for By-Law PDFs ═══")
    results = []

    for meeting in meetings:
        if not meeting.get("package_url"):
            continue

        pdf_path = download_pdf(meeting["package_url"], "temp_pdfs/packages")
        
        # Fallback: try council archive mirror
        if not pdf_path:
            mirror_url = meeting["package_url"].replace(
                "nipissingtownship.com/wp-content/uploads/",
                f"council.chriswjohnston.ca/{meeting['year']}/files/"
            )
            # Simplify the filename for the mirror
            pdf_path = download_pdf(mirror_url, "temp_pdfs/packages")
        
        if not pdf_path:
            continue

        # Try text extraction first (works for most packages)
        text = extract_pdf_text(pdf_path)
        if not text or len(text.strip()) < 100:
            if OCR_AVAILABLE:
                print(f"    {meeting['date']}: No text, trying OCR...")
                text = ocr_pdf(pdf_path)
            if not text or len(text.strip()) < 100:
                print(f"    {meeting['date']}: Package unreadable, skipping")
                continue

        # Find ALL by-law numbers in the package
        nums = set(re.findall(r'By[\-\s]?Law\s*(?:No\.?\s*|Number\s*)?(\d{4}[\-–]\d{1,3})', text, re.IGNORECASE))
        nums = {n.replace("–", "-") for n in nums}

        if not nums:
            continue

        print(f"    {meeting['date_display']}: Found by-laws {', '.join(sorted(nums))}")

        # Extract standalone PDFs for each by-law
        for num in nums:
            extracted = extract_bylaw_pdf(pdf_path, num)
            if extracted:
                results.append({"_pdf_for": num, "_pdf_path": str(extracted)})

        # For by-laws not yet in our database, create entries
        new_nums = nums - known_numbers
        for num in new_nums:
            title = None
            escaped = re.escape(num).replace(r'\-', r'[\-–]')
            tm = re.search(
                rf'[Bb]eing\s+a\s+[Bb]y[\-\s]?[Ll]aw\s+(?:to\s+)?(.{{5,150}}?)[\n\r\.]',
                text[max(0, text.lower().find(num.lower())-200):text.lower().find(num.lower())+500] if num.lower() in text.lower() else '',
                re.IGNORECASE
            )
            if tm:
                title = re.sub(r'\s+', ' ', tm.group(1)).strip()
                if len(title) > 120:
                    title = title[:117] + '...'

            results.append({
                "number": num,
                "year": parse_year(num),
                "title": title or f"By-Law {num}",
                "date_passed": None,
                "pdf_url": None,
                "page_url": None,
                "source": "agenda_package",
                "status": "pending",
                "votes": None,
                "meeting_date": meeting["date"],
                "agenda_package_url": meeting["package_url"],
                "minutes_url": meeting.get("minutes_url"),
            })

    actual = [b for b in results if "number" in b]
    pdfs = [b for b in results if "_pdf_for" in b]
    print(f"  New by-laws found: {len(actual)}")
    print(f"  PDFs extracted: {len(pdfs)}")
    return results


def extract_bylaw_pdf(package_path, bylaw_num):
    """Extract pages belonging to a specific by-law from an agenda package.
    
    By-laws in Nipissing packages follow this pattern:
    - Start: "THE CORPORATION OF THE TOWNSHIP OF NIPISSING" + "BY-LAW NUMBER YYYY-NN"
    - End: Signature block (Mayor / Municipal Administrator) or next by-law header
    """
    doc = fitz.open(str(package_path))
    escaped = re.escape(bylaw_num).replace(r'\-', r'[\-–]')
    pages = []
    in_bl = False

    for i in range(len(doc)):
        text = doc[i].get_text()
        
        # Try OCR if no text
        if (not text or len(text.strip()) < 20) and OCR_AVAILABLE:
            try:
                mat = fitz.Matrix(200 / 72, 200 / 72)
                pix = doc[i].get_pixmap(matrix=mat)
                img = Image.frombytes("RGB", [pix.width, pix.height], pix.samples)
                text = pytesseract.image_to_string(img)
            except Exception:
                text = ""

        if not text:
            if in_bl:
                pages.append(i)  # Continuation page (might be a scanned schedule)
            continue

        # Check if this page starts a by-law matching our number
        has_our_bylaw = bool(re.search(
            rf'BY[\-\s]?LAW\s*(?:NO\.?\s*|NUMBER\s*)?{escaped}',
            text, re.IGNORECASE
        ))
        # Check if this page starts a DIFFERENT by-law
        has_other_bylaw = bool(re.search(
            r'CORPORATION\s+OF\s+THE\s+TOWNSHIP.*?BY[\-\s]?LAW\s*(?:NO\.?\s*|NUMBER\s*)\d{4}[\-–]\d',
            text, re.IGNORECASE | re.DOTALL
        )) and not has_our_bylaw

        if has_our_bylaw and not in_bl:
            in_bl = True
            pages.append(i)
        elif in_bl:
            if has_other_bylaw:
                in_bl = False  # Hit the next by-law
            elif re.search(r'(?:^|\n)\s*\d+[\.\)]\s+[A-Z]', text) and \
                 'BY-LAW' not in text.upper() and \
                 re.search(r'(?:AGENDA|ITEM\s+\d|CORRESPONDENCE)', text, re.IGNORECASE):
                in_bl = False  # Hit a new agenda section
            else:
                pages.append(i)

    if pages:
        year = parse_year(bylaw_num) or "unknown"
        out_dir = PDF_DIR / str(year)
        out_dir.mkdir(parents=True, exist_ok=True)
        out_path = out_dir / f"By-Law-{bylaw_num}.pdf"
        new_doc = fitz.open()
        for p in pages:
            new_doc.insert_pdf(doc, from_page=p, to_page=p)
        new_doc.save(str(out_path))
        new_doc.close()
        doc.close()
        print(f"    Extracted {bylaw_num} ({len(pages)} pages) → {out_path.name}")
        return out_path

    doc.close()
    return None


# ── Step 5: AI Summaries ──────────────────────────────

def generate_ai_summary(bylaw, pdf_text=None):
    if not ANTHROPIC_AVAILABLE:
        return None, None
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        return None, None

    ctx = [f"By-Law Number: {bylaw['number']}", f"Title: {bylaw['title']}"]
    if bylaw.get("year"):
        ctx.append(f"Year: {bylaw['year']}")
    if pdf_text and len(pdf_text.strip()) > 50:
        ctx.append(f"\nFull text:\n{pdf_text[:12000]}")

    prompt = f"""Summarize this municipal by-law for the Township of Nipissing, Ontario.
Plain language a resident can understand.

{chr(10).join(ctx)}

Respond ONLY in JSON:
{{"summary": "2-3 sentence plain-language summary under 60 words.", "key_points": ["Point 1", "Point 2", "Point 3"]}}

Focus on: what's regulated, what's required, penalties. Plain language, no jargon.
3-5 key points, each under 20 words."""

    try:
        client = anthropic.Anthropic(api_key=api_key)
        r = client.messages.create(
            model="claude-sonnet-4-20250514", max_tokens=500,
            messages=[{"role": "user", "content": prompt}]
        )
        txt = r.content[0].text.strip()
        txt = re.sub(r'^```json\s*', '', txt)
        txt = re.sub(r'\s*```$', '', txt)
        parsed = json.loads(txt)
        return parsed.get("summary"), parsed.get("key_points", [])
    except Exception as e:
        print(f"    WARN: AI summary failed for {bylaw['number']}: {e}")
        return None, None


def generate_all_summaries(bylaws):
    if not ANTHROPIC_AVAILABLE or not os.environ.get("ANTHROPIC_API_KEY"):
        print("\n═══ Step 5: AI Summaries — SKIPPED (no API key) ═══")
        return

    need = [b for b in bylaws if not b.get("ai_summary")]
    if not need:
        print("\n═══ Step 5: AI Summaries — all complete ═══")
        return

    MAX_PER_RUN = 20
    batch = need[:MAX_PER_RUN]
    print(f"\n═══ Step 5: Generating AI Summaries ({len(batch)} of {len(need)} remaining) ═══", flush=True)
    done = 0
    for b in batch:
        print(f"  → {b['number']}...", end='', flush=True)
        # Just use title — skip PDF downloads to avoid hangs in CI
        summary, points = generate_ai_summary(b, None)
        if summary:
            b["ai_summary"] = summary
            b["ai_key_points"] = points or []
            done += 1
            print(f" ✓")
        else:
            print(f" ✗")
        time.sleep(0.3)

    print(f"  Generated {done} summaries", flush=True)


# ── Step 6: Resolution Parsing ─────────────────────────

def categorize_resolution(text):
    t = text.lower()
    if re.search(r'minutes.+adopted|adopt.+minutes', t): return 'Minutes Adoption'
    if re.search(r'meeting be adjourned', t): return 'Adjournment'
    if re.search(r'confirm the proceedings', t): return 'Confirming By-Law'
    if re.search(r'statement of accounts|accounts.+approved', t): return 'Accounts Payable'
    if re.search(r'correspondence.+report|receive the correspondence|accept the correspondence', t): return 'Correspondence'
    if re.search(r'by[\-\s]?law', t): return 'By-Law'
    if re.search(r'budget|tax levy|tax rate|tax ratio|capital forecast|estimates and tax rates|financial statement', t): return 'Budget & Finance'
    if re.search(r'tender|quotation|accept the.+proposal|purchase', t): return 'Procurement'
    if re.search(r'appoint|resignation|committee', t): return 'Appointments'
    if re.search(r'whereas.+support|resolution.+support|circulated to', t): return 'Support Resolution'
    if re.search(r'authorize|delegation|attendance|conference|grant|sign.+agreement', t): return 'Authorization'
    if re.search(r'closed.+session|resume.+open|closed to the public', t): return 'Closed Session'
    if re.search(r'donate|waive', t): return 'Donations & Waivers'
    return 'General'


def create_res_title(text, category):
    if category in ('Minutes Adoption', 'Adjournment', 'Correspondence', 'Closed Session'):
        return category
    if category == 'Accounts Payable':
        m = re.search(r'[Tt]otaling\s*\$([\d,]+\.\d{2})', text)
        return f"Accounts Payable – ${m.group(1)}" if m else 'Accounts Payable'
    if category == 'Confirming By-Law':
        return 'Confirming By-Law'
    title = re.sub(r'^(?:THAT|That)\s+', '', text)
    first = re.split(r'[.;]', title)[0].strip()
    if len(first) > 120:
        first = first[:117] + '...'
    return first or category


def parse_resolutions_from_minutes(text, meeting):
    """Parse ALL resolutions from a minutes PDF text."""
    results = []
    blocks = re.split(r'(R\d{4}[\-–]\d{1,3})', text)

    for i in range(1, len(blocks), 2):
        res_num = blocks[i].replace('–', '-')
        if i + 1 >= len(blocks):
            continue
        body = blocks[i + 1].strip()

        # Mover / seconder
        mm = re.match(r'\s*[:.]?\s*([A-Z]\.\s*\w+(?:\s+\w+)?)\s*,\s*([A-Z]\.\s*\w+(?:\s+\w+)?)\s*[:\n]', body)
        mover = mm.group(1).strip() if mm else None
        seconder = mm.group(2).strip() if mm else None

        motion = body[mm.end():].strip() if mm else body
        cp = motion.rfind('Carried')
        if cp > 0:
            motion = motion[:cp].strip()
        motion = re.sub(r'\s+', ' ', motion).rstrip('. \n')

        status = 'carried' if 'Carried' in body else 'defeated' if re.search(r'Defeated|Lost|Failed', body) else 'unknown'

        bylaw_match = re.search(r'By[\-\s]?Law\s*(?:No\.?\s*|Number\s*)?(\d{4}[\-–]\d{1,3})', motion, re.IGNORECASE)
        category = categorize_resolution(motion)
        title = create_res_title(motion, category)
        votes = f"Moved by {mover}, Seconded by {seconder}" if mover and seconder else None

        results.append({
            'number': res_num,
            'title': title,
            'motion_text': motion[:500] if len(motion) > 500 else motion,
            'meeting_date': meeting['date'],
            'minutes_url': meeting.get('minutes_url'),
            'status': status,
            'votes': votes,
            'mover': mover,
            'seconder': seconder,
            'is_bylaw': bool(bylaw_match),
            'bylaw_number': bylaw_match.group(1).replace('–', '-') if bylaw_match else None,
            'category': category,
        })

    return results


# ── Merge ──────────────────────────────────────────────

def merge(existing, new_list):
    idx = {b["number"]: b for b in existing}
    for b in new_list:
        if "_pdf_for" in b:
            # This is a PDF extraction result, attach to existing
            num = b["_pdf_for"]
            if num in idx and not idx[num].get("pdf_url"):
                rel = b["_pdf_path"]
                idx[num]["pdf_url"] = rel
            continue

        num = b["number"]
        if num in idx:
            old = idx[num]
            for key in ("title", "pdf_url", "page_url", "votes", "meeting_date",
                        "minutes_url", "agenda_package_url", "ai_summary", "ai_key_points"):
                if not old.get(key) and b.get(key):
                    old[key] = b[key]
                # Special case: upgrade title from generic
                if key == "title" and old.get("title", "").startswith("By-Law ") and \
                   b.get("title") and not b["title"].startswith("By-Law "):
                    old["title"] = b["title"]
            if old.get("status") == "pending" and b.get("status") in ("approved", "defeated"):
                old["status"] = b["status"]
            if not old.get("date_passed") and b.get("date_passed"):
                old["date_passed"] = b["date_passed"]
        else:
            idx[num] = b

    return list(idx.values())


# ── Main ───────────────────────────────────────────────

def run():
    print("=" * 60)
    print("Nipissing Township By-Law Scraper v2")
    print(f"Run: {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    print(f"OCR: {'yes' if OCR_AVAILABLE else 'no (install pytesseract)'}")
    print(f"AI:  {'yes' if ANTHROPIC_AVAILABLE and os.environ.get('ANTHROPIC_API_KEY') else 'no'}")
    print("=" * 60)

    data = load_data()
    existing = data.get("bylaws", [])
    print(f"Existing: {len(existing)} by-laws")

    # Step 1
    page_bylaws = scrape_bylaws_page()

    # Step 2
    meetings = scrape_council_meetings()

    # Step 3 — the main source
    minutes_bylaws = scrape_all_minutes(meetings)

    # Step 4 — SKIPPED: agenda package PDF extraction is too slow for CI (OCR on large PDFs).
    # By-laws are already found via minutes in Step 3. Agenda package extraction can be
    # run locally or re-enabled with a per-package time limit later.
    print("\n═══ Step 4: Agenda Package Scanning — SKIPPED (CI mode) ═══")
    print("  Run locally for full agenda package extraction: python scraper/scrape.py")
    package_bylaws = []

    # Merge
    print("\n═══ Merging ═══", flush=True)
    all_bl = merge(existing, page_bylaws)
    all_bl = merge(all_bl, minutes_bylaws)
    all_bl = merge(all_bl, package_bylaws)
    all_bl.sort(key=lambda b: (b.get("year") or 0, b.get("number", "")))
    data["bylaws"] = all_bl

    # Step 5 — SKIPPED for now
    print("\n═══ Step 5: AI Summaries — SKIPPED ═══", flush=True)

    # Step 6: Extract resolutions from minutes
    print("\n═══ Step 6: Extracting Resolutions from Minutes ═══", flush=True)
    res_data = load_resolutions()
    existing_res = {r["number"]: r for r in res_data.get("resolutions", [])}
    new_res_count = 0

    for meeting in meetings:
        if not meeting.get("minutes_url"):
            continue
        if meeting.get("date") == "special-meetings":
            continue  # Handle below

        minutes_type = meeting.get("minutes_type", "pdf")
        text = None

        if minutes_type == "html":
            try:
                soup = fetch_page(meeting["minutes_url"])
                content = soup.find("div", class_="entry-content") or soup.find("article")
                if content:
                    text = content.get_text(separator="\n", strip=True)
            except Exception:
                continue
        else:
            pdf_path = download_pdf(meeting["minutes_url"], "temp_pdfs/minutes")
            if not pdf_path:
                continue
            text = extract_pdf_text(pdf_path)

        if not text or len(text.strip()) < 100:
            continue

        resolutions = parse_resolutions_from_minutes(text, meeting)
        for r in resolutions:
            if r["number"] not in existing_res:
                existing_res[r["number"]] = r
                new_res_count += 1
            else:
                old = existing_res[r["number"]]
                for k in ("title", "votes", "motion_text", "minutes_url", "meeting_date", "category"):
                    if not old.get(k) and r.get(k):
                        old[k] = r[k]

    # Parse the special meetings page too
    special = [m for m in meetings if m.get("date") == "special-meetings"]
    if special:
        try:
            soup = fetch_page(special[0]["minutes_url"])
            content = soup.find("div", class_="entry-content") or soup.find("article")
            if content:
                text = content.get_text(separator="\n", strip=True)
                resolutions = parse_resolutions_from_minutes(text, special[0])
                for r in resolutions:
                    if r["number"] not in existing_res:
                        existing_res[r["number"]] = r
                        new_res_count += 1
        except Exception:
            pass

    all_res = sorted(existing_res.values(), key=lambda r: r.get("number", ""))
    res_data["resolutions"] = all_res
    res_data["last_updated"] = datetime.now().strftime("%Y-%m-%d")
    RES_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(RES_FILE, "w") as f:
        json.dump(res_data, f, indent=2, ensure_ascii=False)
    print(f"  Total resolutions: {len(all_res)} ({new_res_count} new)")

    # Save by-laws
    save_data(data)

    # Report
    print("\n" + "=" * 60)
    print("SUMMARY")
    print("=" * 60)
    print(f"Total by-laws: {len(all_bl)}")
    print(f"  Approved:     {sum(1 for b in all_bl if b.get('status') == 'approved')}")
    print(f"  Pending:      {sum(1 for b in all_bl if b.get('status') == 'pending')}")
    print(f"  With PDF:     {sum(1 for b in all_bl if b.get('pdf_url'))}")
    print(f"  With votes:   {sum(1 for b in all_bl if b.get('votes'))}")
    print(f"  With summary: {sum(1 for b in all_bl if b.get('ai_summary'))}")
    print(f"\n  Resolutions:  {len(all_res)}")
    years = sorted(set(b.get("year") for b in all_bl if b.get("year")))
    for y in years:
        c = sum(1 for b in all_bl if b.get("year") == y)
        print(f"    {y}: {c}")
    legacy = sum(1 for b in all_bl if not b.get("year"))
    if legacy:
        print(f"    Legacy: {legacy}")

    # Cleanup
    import shutil
    if Path("temp_pdfs").exists():
        shutil.rmtree("temp_pdfs")
        print("\nCleaned up temp downloads")

    print("\nDone!")


if __name__ == "__main__":
    run()
