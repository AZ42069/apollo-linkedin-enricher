#!/usr/bin/env python3
"""
apollo_to_linkedin.py

Enrich an Apollo contact list with LinkedIn profile URLs, then output a CSV
ready to upload into the LinkedIn outreacher (linkedin-outreach-ten.vercel.app).

INPUT CSV  (Apollo export / op-scraper-run apollo_targeting_people.csv)
  Required columns: first_name, company
  Optional columns: last_initial, title

OUTPUT CSV  (paste straight into the outreacher)
  first_name, last_name, linkedin_url, firm_name, title

HOW IT WORKS
  For each unique company it calls LinkedIn's company-people page via the
  mcp-server-linkedin MCP server (same server used by the lead-engine pipeline).
  Employee references include /in/username/ paths, which we capture here.
  People are matched to employees by first_name + last_initial (when available).

USAGE
  python apollo_to_linkedin.py input.csv
  python apollo_to_linkedin.py input.csv output.csv
  python apollo_to_linkedin.py input.csv --pace 5.0 --max-companies 300

  Input may also be a plain list of company names (one per line, no header)
  in which case all employees found are written to the output CSV.

ENV VARS
  LINKEDIN_PACE_SECONDS    delay between MCP calls, default 4.0
  LINKEDIN_MAX_COMPANIES   safety cap, default 500
"""

import asyncio, csv, json, os, re, sys, argparse
from pathlib import Path

try:
    from fastmcp import Client
except ImportError:
    sys.exit("fastmcp not installed — run: pip install fastmcp")

# ── Config ───────────────────────────────────────────────────────────────────
UVX = "/opt/homebrew/bin/uvx"   # adjust if uvx lives elsewhere
SERVER = {"mcpServers": {"linkedin": {"command": UVX, "args": ["mcp-server-linkedin@latest"]}}}
DEFAULT_PACE = float(os.environ.get("LINKEDIN_PACE_SECONDS", "4.0"))
DEFAULT_MAX  = int(os.environ.get("LINKEDIN_MAX_COMPANIES", "500"))

# ── Normalization ─────────────────────────────────────────────────────────────
_CO_STOP = {"llc","inc","corp","corporation","co","company","ltd","limited","gmbh",
            "plc","lp","llp","group","holdings","international","intl","usa","the",
            "incorporated","srl","sa","ag","bv","nv","oy","ab","spa","s","p"}
_NAME_STOP = {"jr","sr","ii","iii","iv","v","phd","md","pe","cpa","esq","mba",
              "dr","mr","ms","mrs","prof"}

def norm_co(c):
    toks = [t for t in re.sub(r"[^a-z0-9 ]", " ", (c or "").lower()).split()
            if t and t not in _CO_STOP]
    return " ".join(toks)

def name_parts(full):
    """Return (first, first-surname-tok, last-tok) or None if < 2 tokens."""
    toks = [t for t in re.sub(r"[^a-z ]", " ", (full or "").lower()).split()
            if t and t not in _NAME_STOP]
    return (toks[0], toks[1], toks[-1]) if len(toks) >= 2 else None

# ── MCP helpers ───────────────────────────────────────────────────────────────
def _to_obj(res):
    """Unwrap a fastmcp CallToolResult to a plain dict."""
    for attr in ("data", "structured_content"):
        d = getattr(res, attr, None)
        if isinstance(d, dict):
            return d.get("result", d) if isinstance(d.get("result"), dict) else d
    for c in (getattr(res, "content", None) or []):
        t = getattr(c, "text", None)
        if t:
            try: return json.loads(t)
            except Exception: pass
    return {}

def _pick_slug(obj, company):
    refs = (obj.get("references") or {}).get("search_results") or []
    cands = []
    for r in refs:
        if r.get("kind") != "company": continue
        m = re.search(r"/company/([^/]+)/?", r.get("url", ""))
        if m: cands.append((r.get("text", ""), m.group(1)))
    if not cands: return None
    nc = norm_co(company)
    for text, slug in cands:
        if norm_co(text) == nc: return slug
    for text, slug in cands:
        if nc and (nc in norm_co(text) or norm_co(text) in nc): return slug
    return cands[0][1]

def _parse_employees(obj):
    """Return list of {name, title, linkedin_url} from get_company_employees result."""
    refs = (obj.get("references") or {}).get("employees") or []
    names_seen = set()
    url_of = {}
    name_order = []
    for r in refs:
        if r.get("kind") != "person": continue
        name = (r.get("text") or "").strip()
        if not name or name in names_seen: continue
        names_seen.add(name)
        name_order.append(name)
        raw_url = r.get("url", "")
        if raw_url.startswith("/in/"):
            url_of[name] = "https://www.linkedin.com" + raw_url.rstrip("/") + "/"

    # Extract titles from the freetext section
    SKIPWORDS = ("connect","follow","message","followers","follower",
                 "page 1","previous","next","show more","people you may know")
    section_text = (obj.get("sections") or {}).get("employees") or ""
    lines = [l.strip() for l in section_text.splitlines() if l.strip()]
    title_of = {}
    for i, line in enumerate(lines):
        if line in names_seen and i + 1 < len(lines):
            nxt = lines[i + 1]
            if not any(w in nxt.lower() for w in SKIPWORDS):
                title_of.setdefault(line, nxt)

    return [{"name": n, "title": title_of.get(n, ""), "linkedin_url": url_of.get(n, "")}
            for n in name_order]

# ── Matching ──────────────────────────────────────────────────────────────────
_TITLE_STOP = {"of","the","and","at","for","to","a","an","&","-","|"}

def _title_toks(t):
    return {w for w in re.sub(r"[^a-z ]", " ", (t or "").lower()).split()
            if w and w not in _TITLE_STOP}

def _match(person, employees):
    """Return best-matching employee dict or None (high-confidence only)."""
    first = (person.get("first_name") or "").strip().lower()
    li_raw = (person.get("last_initial") or "").strip().lower()
    li = li_raw[0] if li_raw else ""

    if not first:
        return None

    cands = []
    for emp in employees:
        np = name_parts(emp["name"])
        if not np: continue
        fn, sf, sl = np
        if fn != first: continue
        # If we have a last initial, require it to match
        if li and not (sf.startswith(li) or sl.startswith(li)): continue
        cands.append(emp)

    if not cands: return None
    if len(cands) == 1: return cands[0]

    # Disambiguate by title-token overlap
    at = _title_toks(person.get("title"))
    scored = sorted(cands, key=lambda c: len(at & _title_toks(c["title"])), reverse=True)
    best  = len(at & _title_toks(scored[0]["title"]))
    second = len(at & _title_toks(scored[1]["title"])) if len(scored) > 1 else 0
    return scored[0] if best > 0 and best > second else None

# ── Fetch loop ────────────────────────────────────────────────────────────────
async def _fetch_companies(companies, cache_path, pace):
    """Fetch employee data for each company, writing to cache_path as we go.
    Returns cache dict: norm_co -> employee_list."""
    cache = {}
    if os.path.exists(cache_path):
        for line in open(cache_path):
            try:
                d = json.loads(line)
                cache[norm_co(d["company"])] = d.get("employees", [])
            except Exception:
                pass
        if cache:
            print(f"  Resuming — {len(cache)} companies already cached")

    todo = [c for c in companies if norm_co(c) not in cache]
    if not todo:
        print("  All companies already in cache — skipping fetch")
        return cache

    print(f"  Fetching {len(todo)} companies from LinkedIn (pace {pace}s)…")
    fp = open(cache_path, "a")
    async with Client(SERVER) as client:
        for i, company in enumerate(todo, 1):
            employees = []
            try:
                s = _to_obj(await client.call_tool("search_companies", {"keywords": company}))
                slug = _pick_slug(s, company)
                if slug:
                    await asyncio.sleep(pace)
                    e = _to_obj(await client.call_tool("get_company_employees", {"company_name": slug}))
                    employees = _parse_employees(e)
            except Exception as ex:
                print(f"    [{i}/{len(todo)}] WARN {company}: {str(ex)[:80]}")

            cache[norm_co(company)] = employees
            fp.write(json.dumps({"company": company, "employees": employees}) + "\n")
            fp.flush()
            if i % 10 == 0 or i == len(todo):
                total_ppl = sum(len(v) for v in cache.values())
                print(f"    {i}/{len(todo)} companies | {total_ppl} employees cached")
            await asyncio.sleep(pace)
    fp.close()
    return cache

# ── Main ──────────────────────────────────────────────────────────────────────
async def run(input_path, output_path, pace, max_companies):
    input_path = Path(input_path)
    output_path = Path(output_path)
    cache_path  = output_path.with_suffix(".cache.jsonl")

    # ── Parse input ───────────────────────────────────────────────────────────
    raw = input_path.read_text()
    lines = [l.strip() for l in raw.splitlines() if l.strip()]
    if not lines:
        sys.exit("Input file is empty")

    first_line = lines[0]
    has_header = "," in first_line and re.search(r"company|first_name", first_line, re.I)
    people = []
    company_only_mode = False   # just a list of company names

    if has_header:
        people = list(csv.DictReader(raw.splitlines()))
        # Normalise column names to lower
        people = [{k.strip().lower(): v for k, v in row.items()} for row in people]
    elif "," not in first_line:
        # Plain list of company names
        company_only_mode = True
        people = [{"company": l, "first_name": "", "last_initial": "", "title": ""} for l in lines]
    else:
        sys.exit("Could not detect input format — need a CSV with headers or one company name per line")

    print(f"Input: {len(people)} rows | company-only mode: {company_only_mode}")

    # ── Unique companies (order-preserving, capped) ───────────────────────────
    seen = set(); companies = []
    for p in people:
        c = (p.get("company") or "").strip()
        k = norm_co(c)
        if c and k not in seen:
            seen.add(k); companies.append(c)
    companies = companies[:max_companies]
    print(f"Unique companies: {len(companies)} (cap {max_companies})")

    # ── Fetch ─────────────────────────────────────────────────────────────────
    cache = await _fetch_companies(companies, cache_path, pace)

    # ── Match & build output ──────────────────────────────────────────────────
    rows = []
    matched = unmatched = no_url = 0

    if company_only_mode:
        # Emit all employees found
        for company in companies:
            for emp in cache.get(norm_co(company), []):
                parts = emp["name"].strip().split()
                rows.append({
                    "first_name":   parts[0] if parts else "",
                    "last_name":    " ".join(parts[1:]) if len(parts) > 1 else "",
                    "linkedin_url": emp["linkedin_url"],
                    "firm_name":    company,
                    "title":        emp["title"],
                })
                if not emp["linkedin_url"]: no_url += 1
    else:
        for p in people:
            emps = cache.get(norm_co(p.get("company", "")), [])
            emp  = _match(p, emps) if emps else None
            if emp:
                parts = emp["name"].strip().split()
                rows.append({
                    "first_name":   parts[0] if parts else (p.get("first_name") or ""),
                    "last_name":    " ".join(parts[1:]) if len(parts) > 1 else "",
                    "linkedin_url": emp["linkedin_url"],
                    "firm_name":    p.get("company", ""),
                    "title":        emp["title"] or p.get("title", ""),
                })
                if not emp["linkedin_url"]: no_url += 1
                matched += 1
            else:
                unmatched += 1

    # Dedupe on linkedin_url
    seen_urls = set(); deduped = []
    for r in rows:
        k = r["linkedin_url"].lower() if r["linkedin_url"] else id(r)
        if k not in seen_urls:
            seen_urls.add(k); deduped.append(r)

    with open(output_path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["first_name","last_name","linkedin_url","firm_name","title"])
        w.writeheader()
        w.writerows(deduped)

    print(f"\nResults:")
    if not company_only_mode:
        print(f"  Matched:   {matched}")
        print(f"  Unmatched: {unmatched}")
    print(f"  Output rows: {len(deduped)}")
    print(f"  With LinkedIn URL: {len(deduped) - no_url} / {len(deduped)}")
    print(f"  Output: {output_path}")
    print(f"  Cache:  {cache_path}")


def main():
    parser = argparse.ArgumentParser(
        description="Enrich Apollo contacts with LinkedIn URLs for the LinkedIn outreacher"
    )
    parser.add_argument("input",  help="Input CSV (Apollo export) or plain company-name list")
    parser.add_argument("output", nargs="?", help="Output CSV (default: <input>_linkedin.csv)")
    parser.add_argument("--pace", type=float, default=DEFAULT_PACE,
                        help=f"Seconds between LinkedIn MCP calls (default {DEFAULT_PACE})")
    parser.add_argument("--max-companies", type=int, default=DEFAULT_MAX,
                        help=f"Max companies to process (default {DEFAULT_MAX})")
    args = parser.parse_args()

    inp = Path(args.input)
    if not inp.exists():
        sys.exit(f"Input file not found: {inp}")

    if args.output:
        out = Path(args.output)
    else:
        out = inp.with_stem(inp.stem + "_linkedin").with_suffix(".csv")

    asyncio.run(run(str(inp), str(out), args.pace, args.max_companies))


if __name__ == "__main__":
    main()
