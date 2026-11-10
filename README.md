# OrgGraph: org chart inference from flat employee lists

Sales teams ask things like *"who runs the data team here?"*, *"who does the Head of Security report to?"* and *"who is the economic buyer above this champion?"*. The data only has a flat list of people and job titles for each company. OrgGraph infers the reporting lines, puts a confidence on every edge, and leaves a manager **empty** when it can't be sure, because a wrong edge is worse than no edge.

It is a fully static site. The browser downloads `data/orgchart.db` and queries it with [sql.js](https://github.com/sql-js/sql.js) (SQLite compiled to WebAssembly). There is no backend, so it runs as-is on GitHub Pages.

## Results (held-out companies)

| Metric | Value |
|---|---|
| Edge precision | **98.6%** (304 wrong edges out of 22,300) |
| People with a direct manager edge | 69.5% |
| People placed by an edge or a roll-up | 82.8% |
| Roll-up precision | 96.8% |
| Duplicate rows caught | 100% |

The rest are left unresolved on purpose. The app lists their candidate managers but never asserts one.

## Repo layout

```
index.html            app shell
css/styles.css        styles (light and dark)
js/app.js             all UI logic: SQL queries, tree, person panel, quality page
vendor/sql-wasm.*     sql.js 1.13.0 (bundled, so there are no CDN dependencies)
data/orgchart.db      SQLite database: raw data, answer key and inference output
generate_data.py      synthetic data generator (Faker)
infer.py              offline inference and calibration
schema.sql            raw-data schema
requirements.txt
```

## Run locally

```bash
python3 -m http.server 8000      # then open http://localhost:8000
```

Opening `index.html` straight from disk won't work, because browsers block `fetch()` on `file://` URLs.

## Regenerate the data

```bash
pip install -r requirements.txt
python generate_data.py          # 520 companies, 100-150 people each -> data/orgchart.db
python infer.py                  # writes the inference tables into the same file
python infer.py --min-precision 0.95   # stricter: fewer edges, higher precision
```

The generator is seeded (`--seed 42`), so runs are reproducible.

## Deploy to GitHub Pages

1. Push this folder to a repo. The `.db` file is about 24 MB, well under GitHub's 100 MB file limit, so Git LFS isn't needed.
2. Go to **Settings → Pages → Build and deployment**, set **Source** to *Deploy from a branch*, and pick `main` and `/ (root)`.
3. The site will be live at `https://<user>.github.io/<repo>/`. The `.nojekyll` file stops Jekyll from processing the files.

## How the data is generated

For each company, the generator first builds a **true** hierarchy: CEO, C-suite, VPs, heads, directors, managers and ICs across 14 functions, with the mix of functions depending on the industry (12 industries in total). Sub-functions such as Data, Security, Design, IT and Support usually sit under a parent leader. A few lines are rewired in ways titles can't reveal, for example Marketing under the CRO, or Customer Success under the COO. Then it keeps only a flat, messy list:

- **Abbreviations and reordering:** `Sr. SWE | Payments`, `Mgr, Sales, EMEA`, `Vice Pres., Marketing`
- **Formatting noise:** typos (`Enginner`, `Manger`), ALL CAPS and lowercase, emojis, `| We're hiring!`, `Ex-Google`, `, MBA`, and `Interim`/`Acting`/`Founding` prefixes
- **Foreign titles:** `Vertriebsleiter`, `Chef de produit`, `Ingeniero de Software`
- **Missing and useless values:** missing titles (about 1.5%), titles like `Team Member`, `N/A` or `Manager`, missing departments (28%) and locations (14%)
- **Inconsistent departments:** `GTM`, `R&D`, `People Ops`, `G&A`
- **Inconsistent cities:** `SF`, `Bangalore`, `Greater London`
- **Duplicates:** about 0.6% duplicate rows, and names written as `LAST, First`
- **Traps:** Product Manager, Account Manager and Customer Success Manager are *not* people managers

The answer key is stored in the `ground_truth` table. Inference never reads it. It is only used for calibration and scoring.

## How inference works (`infer.py`)

1. **Normalise titles:** strip noise, expand abbreviations, fix typos with fuzzy matching against a vocabulary, and translate common foreign titles.
2. **Parse each title** into:
   - a **level**: CEO 9, C-suite 8, SVP 7.5, VP 7, Head 6, Director 5, Manager 4, Senior IC 2, IC 1
   - a **function**, falling back to the messy department field
   - **team qualifiers**, i.e. words that aren't role words, such as `payments`, `core services` or `emea`
3. **Deduplicate** people by normalised name or email within a company.
4. **Resolve managers top-down.** Only manager-tier people (level 4 and up) can have reports.
   - Candidates are people in the same function at the nearest level above.
   - With one candidate, it is the manager. With several, the tie is broken by a shared team qualifier, then by the same city.
   - A function head attaches to its parent function's leader (Data/Security → Engineering, Design → Product, Support → Customer Success, IT → Operations) or to the CEO.
   - C-suite, Chief of Staff and EAs report to the CEO.
5. **Calibrate:** each decision is tagged with the rule that produced it (for example `team-match:f-title:L1`). A rule's precision on the even-numbered companies becomes the **confidence** of its edges. A rule must reach ≥90% precision and ≥20 samples to write edges at all. All metrics are reported on the odd-numbered (held-out) companies.
6. **Don't guess:** when several managers are plausible but all report to the same leader, the person gets a **roll-up** to that leader instead of a direct edge, and the plausible managers are stored in `manager_candidates`.

## Database tables

| Table | What it holds |
|---|---|
| `companies`, `employees` | raw input (flat list) |
| `ground_truth` | generator's answer key (scoring only) |
| `people_inferred` | normalised title, function, level, team qualifiers, city, status (`top` / `resolved` / `rollup` / `unresolved` / `duplicate`) |
| `reporting_edges` | accepted manager edges with `confidence`, `rule`, `reason` |
| `rollups` | "rolls up to" links for people with an ambiguous direct manager |
| `manager_candidates` | plausible managers that were *not* asserted |
| `inference_rules` | every rule with calibration and held-out precision |
| `metrics` | headline numbers |

## Using the app

- **Companies:** filter by name or industry, and sort by how much of the chart was resolved.
- **Who runs what:** the most senior person found in each function.
- **Org chart:** a collapsible tree with confidence badges. Dashed "roll-up" lines mean *somewhere under this leader*.
- **Person panel:** manager with the reason and rule precision, the chain to the CEO, direct reports, the **economic buyer** (first VP or C-level above them), and how the title was parsed.
- **Show answer key:** marks each edge ✓ or ✗ against the synthetic ground truth.
- **Model quality:** metrics, every rule's precision, and the highest-confidence mistakes.
