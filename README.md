# apollo-linkedin-enricher

Enrich an Apollo contact list with LinkedIn profile URLs, then output a CSV ready to upload into [linkedin-outreach](https://linkedin-outreach-ten.vercel.app).

## How it works

For each unique company in your Apollo export, the script calls LinkedIn's company-people page via `mcp-server-linkedin` (the same MCP server used by the lead-engine pipeline). Employee profile references include `/in/username/` paths. People are matched back to your Apollo targets by `first_name` + `last_initial`.

**Runs entirely on the Mac mini — no external API, no per-call cost.**

## Requirements

- Mac mini with `mcp-server-linkedin` installed (`uvx` at `/opt/homebrew/bin/uvx`)
- Authenticated LinkedIn session in `~/.linkedin-mcp/profile`
- Python 3.9+ with `fastmcp` installed

```bash
pip install fastmcp
```

## Usage

```bash
# Apollo CSV → LinkedIn-enriched CSV
python apollo_to_linkedin.py apollo_targeting_people.csv

# Custom output path
python apollo_to_linkedin.py apollo_targeting_people.csv outreach_batch.csv

# Slower pace (more conservative, less ban risk)
python apollo_to_linkedin.py apollo_targeting_people.csv --pace 6.0

# Plain list of company names (emits all employees found)
python apollo_to_linkedin.py companies.txt
```

## Input format

Apollo export CSV with at minimum a `company` column. Matching improves with `first_name` and `last_initial`:

```
first_name,last_initial,company,title
Jane,D,Acme Corp,VP Sales
John,S,Globex,CEO
```

Or a plain text file — one company name per line (no header). All employees found are written to output.

## Output format

Paste directly into the LinkedIn outreacher's prospect upload:

```
first_name,last_name,linkedin_url,firm_name,title
Jane,Doe,https://www.linkedin.com/in/jane-doe/,Acme Corp,VP Sales
```

## Resumable

The script writes a `.cache.jsonl` file alongside the output. If interrupted, re-run the same command — already-fetched companies are skipped automatically.

## Pace & limits

Default: 4s between calls, 500 companies max per run. Adjust with `--pace` and `--max-companies`. For 2,400 people across ~400 companies expect ~27 minutes.

```bash
# Override via env vars
LINKEDIN_PACE_SECONDS=5.0 LINKEDIN_MAX_COMPANIES=300 python apollo_to_linkedin.py input.csv
```
