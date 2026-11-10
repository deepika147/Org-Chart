#!/usr/bin/env python3
"""
Offline org chart inference.

Reads the flat `employees` table, and writes back into the same SQLite file:

  people_inferred     cleaned title, function, seniority level, team qualifiers, city
  reporting_edges     accepted manager -> report edges, with a calibrated confidence and reason
  rollups             for people whose direct manager is ambiguous: a leader they certainly roll up to
  manager_candidates  the plausible managers we could not choose between (not edges!)
  inference_rules     every decision rule with its measured precision on held-out companies
  metrics             headline numbers for the Model Quality page

Principle: a wrong edge is worse than no edge. Each decision is tagged with the
rule that produced it; rules are calibrated on a training split of companies
(against the generator's hidden ground truth) and an edge is only written when
its rule's precision clears --min-precision. Everything else is left unresolved.

Usage:
    python infer.py                      # uses data/orgchart.db
    python infer.py --db data/orgchart.db --min-precision 0.9
"""
import argparse
import os
import re
import sqlite3
import unicodedata
from collections import Counter, defaultdict
from difflib import get_close_matches

# ---------------------------------------------------------------------------
# 1. Title normalisation
# ---------------------------------------------------------------------------
NOISE_PATTERNS = [
    r"we'?re hiring!?", r"open to (new )?opportunities", r"building the future of work",
    r"\bex[- ]\w+", r"\(remote\)", r"\(contract\)", r",?\s*\b(mba|phd|cpa)\b", r"\b(interim|acting|founding)\b",
]

# whole-title translations (accents stripped, lowercase)
FOREIGN = {
    "ingeniero de software": "software engineer", "softwareentwickler": "software engineer",
    "ingenieur logiciel": "software engineer", "gerente de ventas": "sales manager",
    "vertriebsleiter": "sales manager", "responsable commercial": "sales manager",
    "ejecutivo de cuentas": "account executive", "charge d'affaires": "account executive",
    "chef de produit": "product manager", "produktmanager": "product manager",
    "directeur commercial": "head of sales", "leiter vertrieb": "head of sales",
    "analista de datos": "data analyst", "datenanalyst": "data analyst", "reclutador": "recruiter",
    "recruteur": "recruiter", "contador": "accountant", "comptable": "accountant", "buchhalter": "accountant",
}

ABBREV = {
    "sr": "senior", "snr": "senior", "jr": "junior", "mgr": "manager", "mngr": "manager",
    "dir": "director", "engr": "engineer", "engg": "engineering", "swe": "software engineer",
    "ops": "operations", "dev": "development", "mktg": "marketing", "rep": "representative",
    "ae": "account executive", "hr": "human resources", "infosec": "information security",
    "assoc": "associate", "pres": "president", "tech": "technology", "v.p": "vp", "csm": "customer success manager",
    "sdr": "sales development representative", "bdr": "business development representative",
    "ea": "executive assistant", "cs": "customer success", "cx": "customer experience",
}

VOCAB = set("""
senior junior staff principal lead head chief officer vice president vp svp evp director manager
engineer engineering software development developer data analytics analyst scientist science machine learning
security information product products design designer ux ui research researcher sales account executive
marketing growth content writer customer success support service representative specialist finance financial
accountant accounting people human resources recruiter recruiting talent legal counsel paralegal compliance
operations operating business technology technical intern associate assistant coordinator administrator
manager team group regional enterprise strategic platform payments mobile infrastructure frontend backend
founder ceo cto cfo coo cmo cro cpo chro ciso cio cdo general deputy controller executive revenue
""".split())

# Words that describe the *role* (function/level), as opposed to the team/area qualifier.
ROLE_WORDS = set("""
senior junior staff principal lead head chief officer vice president vp svp evp director manager management
engineer engineering software development developer data analytics analyst scientist science machine learning ml
security information product design designer ux ui research researcher sales account accounts executive
marketing marketer growth content writer customer success support service representative specialist
finance financial accountant accounting people human resources recruiter recruiting talent acquisition legal
counsel paralegal compliance operations operating business technology technical intern associate assistant
coordinator administrator team group regional founder co cofounder ceo cto cfo coo cmo cro cpo chro ciso cio cdo
general deputy controller of the and to for in at a an i ii iii iv owner it systems system network help desk
technician site reliability devops qa full stack frontend backend mobile bi analytics fp payroll payable billing
tax procurement project program office social media seo graphic visual brand development representative
solutions implementation consultant onboarding renewals generalist partner contracts commercial grc application
appsec engineer's member individual contributor employee n a
""".split())

STOP = {"of", "the", "and", "to", "for", "in", "at", "a", "an", "&"}


def strip_accents(s):
    return "".join(ch for ch in unicodedata.normalize("NFKD", s) if not unicodedata.combining(ch))


def normalize_title(raw):
    """Return (clean_text, tokens, qualifier_text_after_separator)."""
    if not raw:
        return "", []
    t = strip_accents(raw)
    t = "".join(ch for ch in t if unicodedata.category(ch)[0] in "LNPZ" or ch in "&-|/,()")
    t = t.lower().strip()
    for pat in NOISE_PATTERNS:
        t = re.sub(pat, " ", t)
    t = re.sub(r"\s+", " ", t).strip(" -|,/")
    # translate foreign titles (prefix match so "chef de produit - growth" works)
    for k, v in FOREIGN.items():
        if t.startswith(k):
            t = v + t[len(k):]
            break
    t = t.replace("&", " and ").replace("fp and a", "fpa").replace("l and d", "ld").replace("r and d", "rd")
    t = t.replace("v.p.", "vp").replace("co-founder", "cofounder").replace("co founder", "cofounder")
    raw_tokens = re.findall(r"[a-z0-9']+|[,|()/-]", t)
    tokens = []
    for tok in raw_tokens:
        if tok in ",|()/-":
            tokens.append(",")
            continue
        tok = tok.strip("'.")
        if tok in ABBREV:
            tokens.extend(ABBREV[tok].split())
            continue
        if tok == "eng":
            tokens.append("engineer")       # resolved to engineering below if followed by manager/director
            continue
        if len(tok) >= 5 and tok not in VOCAB and tok not in ROLE_WORDS:
            m = get_close_matches(tok, VOCAB, n=1, cutoff=0.84)
            if m:
                tok = m[0]
        tokens.append(tok)
    # "software eng manager" / "director of eng" -> engineering
    for i, tok in enumerate(tokens):
        if tok == "engineer":
            nxt = tokens[i + 1] if i + 1 < len(tokens) else ""
            prv = tokens[i - 1] if i > 0 else ""
            if nxt in ("manager", "director", "lead", "team") or prv in ("of", "director", "vp", "head"):
                tokens[i] = "engineering"
    clean = " ".join(tokens).replace(" ,", ",")
    clean = re.sub(r"(,\s*)+", ", ", clean).strip(", ")
    return clean, tokens


# ---------------------------------------------------------------------------
# 2. Title parsing: level + function + qualifiers
# ---------------------------------------------------------------------------
LEVEL_LABEL = {9: "CEO", 8: "C-suite", 7.5: "SVP", 7: "VP", 6: "Head", 5.5: "Senior Director", 5: "Director",
               4: "Manager", 2: "Senior IC", 1: "IC", 0.5: "Junior / Support", None: "Unknown"}

IC_MANAGER_TITLES = [  # "manager" in the title but not a people manager
    "product manager", "account manager", "customer success manager", "project manager", "program manager",
    "office manager", "marketing manager", "social media manager", "contracts manager", "community manager",
    "product marketing manager", "success manager", "relationship manager",
]
C_SUITE = {"cto": "engineering", "cfo": "finance", "coo": "operations", "cmo": "marketing", "cro": "sales",
           "cpo": None, "chro": "people", "ciso": "security", "cio": "it", "cdo": "data"}
VAGUE = {"manager", "team member", "employee", "consultant", "staff", "associate", "member of technical staff",
         "specialist", "individual contributor", "n a", "analyst", ""}


def parse_level(clean, toks):
    s = " " + clean.replace(",", " ") + " "
    main = clean.split(",")[0].strip()
    if clean.replace(",", "").strip() in VAGUE:
        return None, "vague title"
    if re.search(r"\b(executive assistant|assistant to)\b", s):
        return 0.5, "assistant"
    if " chief of staff " in s:
        return 6, "chief of staff"
    if re.search(r"\b(ceo|chief executive officer)\b", s):
        return 9, "CEO keyword"
    if re.search(r"\bchief \w+( \w+)? officer\b", s) or any(f" {k} " in s for k in C_SUITE) \
            or re.search(r"(?<!deputy )\bgeneral counsel\b", s) or " chief legal " in s:
        return 8, "C-suite keyword"
    if re.search(r"\b(svp|evp|senior vice president|executive vice president)\b", s):
        return 7.5, "SVP keyword"
    if re.search(r"\b(vp|vice president)\b", s):
        return 7, "VP keyword"
    if re.search(r"\bhead of\b|^head\b", clean):
        return 6, "Head-of keyword"
    if re.search(r"\bsenior director\b", s):
        return 5.5, "Senior Director keyword"
    if re.search(r"\b(director|controller|deputy general counsel)\b", s):
        return 5, "Director keyword"
    if re.search(r"\bexecutive assistant\b", s):
        return 0.5, "assistant"
    if re.search(r"\b(team lead|team leader)\b", s):
        return 4, "Team lead keyword"
    if re.search(r"\bmanager\b", s):
        # "manager, customer success" -> people manager; "customer success manager" -> IC
        if main.startswith("manager") or main.startswith("senior manager"):
            return 4, "Manager-first title"
        core = re.sub(r"\b(senior|junior|associate|technical|regional|group|lead)\b ", "", main).strip()
        if "group product manager" in main:
            return 4, "Group PM"
        if any(core.endswith(x) or core == x for x in IC_MANAGER_TITLES):
            return 2 if "senior" in main else 1, "IC 'manager' title (account/product/CS ...)"
        return 4, "Manager keyword"
    if re.search(r"\b(senior|staff|principal|lead)\b", s):
        return 2, "senior IC keyword"
    if re.search(r"\b(junior|intern|coordinator)\b", s):
        return 1, "junior IC keyword"
    return 1, "default IC"


FUNCTION_RULES = [  # ordered: first match wins
    ("executive", r"\b(ceo|chief executive|chief of staff|executive assistant|cofounder|founder)\b"),
    ("marketing", r"\b(product marketing|graphic designer|cmo|chief marketing)\b"),
    ("sales", r"\b(solutions engineer|sales engineer|cro|chief revenue|chief sales)\b"),
    ("security", r"\b(security|ciso|grc|appsec|application security)\b"),
    ("data", r"\b(data|analytics|machine learning|ml|bi|cdo)\b"),
    ("it", r"\b(it|information technology|help desk|helpdesk|systems administrator|network engineer|cio)\b"),
    ("support", r"\b(support|customer service)\b"),
    ("customer_success", r"\b(customer success|onboarding|implementation consultant|renewals|chief customer)\b"),
    ("design", r"\b(design|designer|ux|ui|user research)\b"),
    ("product", r"\b(product|cpo)\b"),
    ("people", r"\b(human resources|people|recruiter|recruiting|talent|chro|hr)\b"),
    ("legal", r"\b(legal|counsel|paralegal|compliance|contracts)\b"),
    ("finance", r"\b(finance|financial|accountant|accounting|fpa|cfo|controller|payroll|billing|accounts payable|tax)\b"),
    ("sales", r"\b(sales|account executive|development representative|business development|account manager|commercial)\b"),
    ("marketing", r"\b(marketing|marketer|content|seo|social media|brand|events)\b"),
    ("operations", r"\b(operations|operating|coo|procurement|supply chain|logistics|office manager|project manager|program manager|facilities)\b"),
    ("engineering", r"\b(engineer|engineering|developer|software|sre|site reliability|devops|qa|cto|technical staff|technology|full stack)\b"),
]

DEPT_RULES = [
    ("security", r"secur"), ("data", r"\bdata|analytics|\bbi\b"), ("it", r"\bit\b|information tech|tech ops"),
    ("support", r"support|customer service|\bcx\b"), ("customer_success", r"customer success|\bcs\b|client serv|account manag|customer exp"),
    ("design", r"design|\bux\b|creative"), ("people", r"people|\bhr\b|human|talent"), ("legal", r"legal|compliance"),
    ("finance", r"financ|account|f&a"), ("sales", r"sales|revenue|business dev|commercial|go-to-market|gtm"),
    ("marketing", r"market|growth|brand"), ("operations", r"operation|\bops\b|strategy"),
    ("product", r"^product$|product manag"), ("engineering", r"eng|r&d|tech|software"),
    ("executive", r"executive|leadership|ceo|management"),
]

REGIONS = {"emea": "EMEA", "apac": "APAC", "north america": "North America", "latam": "LATAM"}


def parse_function(clean, dept):
    s = " " + clean.replace(",", " ") + " "
    # role part first (before the first separator) so "manager, sales, emea" uses the full string
    for fn, pat in FUNCTION_RULES:
        if re.search(pat, s):
            return fn, "title"
    if dept:
        d = dept.lower()
        for fn, pat in DEPT_RULES:
            if re.search(pat, d):
                return fn, "department"
    return None, "none"


def qualifiers(clean, toks):
    """Team / area words: everything that isn't a role word (e.g. 'payments', 'core services', 'emea')."""
    q = set()
    s = clean.replace(",", " ")
    for k in REGIONS:
        if k in s:
            q.add(k.replace(" ", "_"))
            s = s.replace(k, " ")
    for tok in re.findall(r"[a-z0-9]+", s):
        if tok not in ROLE_WORDS and tok not in STOP and len(tok) > 1:
            q.add(tok)
    return q


# ---------------------------------------------------------------------------
# 3. Locations
# ---------------------------------------------------------------------------
CITY_ALIASES = {
    "sf": "san francisco", "san francisco bay area": "san francisco", "nyc": "new york", "new york city": "new york",
    "greater new york area": "new york", "bangalore": "bengaluru", "bangalore urban": "bengaluru",
    "gurgaon": "gurugram", "delhi ncr": "gurugram", "greater london": "london", "munchen": "munich",
    "bombay": "mumbai", "greater seattle area": "seattle", "greater boston": "boston",
}
CITY_REGION = {
    "san francisco": "north_america", "new york": "north_america", "austin": "north_america",
    "seattle": "north_america", "boston": "north_america", "chicago": "north_america", "denver": "north_america",
    "atlanta": "north_america", "toronto": "north_america", "vancouver": "north_america",
    "bengaluru": "apac", "hyderabad": "apac", "pune": "apac", "gurugram": "apac", "chennai": "apac", "mumbai": "apac",
    "singapore": "apac", "sydney": "apac", "melbourne": "apac",
}


def normalize_city(loc):
    if not loc:
        return None
    s = strip_accents(loc).lower().strip()
    if any(w in s for w in ("remote", "anywhere", "wfh")):
        return "remote"
    s = s.split(",")[0].strip()
    return CITY_ALIASES.get(s, s)


def city_region(city):
    if not city or city == "remote":
        return None
    return CITY_REGION.get(city, "emea")


def normalize_name(n):
    if not n:
        return ""
    n = strip_accents(n).lower().strip()
    if "," in n:
        last, first = [x.strip() for x in n.split(",", 1)]
        n = f"{first} {last}"
    return re.sub(r"[^a-z ]", "", n)


# ---------------------------------------------------------------------------
# 4. Manager resolution
# ---------------------------------------------------------------------------
PARENT_FUNCTIONS = {
    "data": ["engineering"], "security": ["engineering"], "design": ["product"],
    "it": ["operations", "finance"], "support": ["customer_success", "operations"],
}


def jacc(a, b):
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def resolve_company(people):
    """people: list of dicts for one company (duplicates already removed).
    Returns {emp_id: decision} where decision has kind 'edge' | 'rollup' | 'none' and a rule key."""
    by_fn = defaultdict(list)
    for p in people:
        if p["level"] is not None and p["function"]:
            by_fn[p["function"]].append(p)
    ceos = [p for p in people if p["level"] == 9]
    ceo = ceos[0] if len(ceos) == 1 else None

    decisions = {}
    order = sorted([p for p in people], key=lambda p: -(p["level"] or 0))
    resolved_mgr = {}

    def add(p, kind, rule, target=None, cands=(), note=""):
        decisions[p["id"]] = dict(kind=kind, rule=rule, target=target, cands=list(cands), note=note)
        if kind == "edge":
            resolved_mgr[p["id"]] = target["id"]

    def common_manager(cands):
        ms = {resolved_mgr.get(c["id"]) for c in cands}
        if len(ms) == 1 and None not in ms:
            return next(iter(ms))
        return None

    for p in order:
        lvl, fn = p["level"], p["function"]
        if lvl == 9:
            add(p, "none", "ceo:top" if ceo else "ceo:multiple", note="Top of the company")
            continue
        if lvl is None or fn is None:
            add(p, "none", "unparsed:" + ("vague-title" if lvl is None else "no-function"),
                note="Title too vague to place")
            continue
        src = "f-title" if p["fn_src"] == "title" else "f-dept"

        # executives: C-suite & chief of staff & EA report to the CEO
        if (fn == "executive" or lvl >= 8) and not ceo:
            add(p, "none", "no-unique-ceo", note="No single CEO was identified to anchor the chart")
            continue
        if fn == "executive" or lvl >= 8:
            if lvl >= 8:
                add(p, "edge", f"csuite->ceo:{fn}", ceo, note=f"C-suite title reports to the CEO")
            elif fn == "executive":
                add(p, "edge", "office-of-ceo->ceo", ceo, note="Chief of Staff / EA reports to the CEO")
            continue

        # only manager-tier people (level >= 4) can have reports; senior ICs never do
        above_same = [c for c in by_fn.get(fn, []) if c["level"] > lvl and c["level"] >= 4 and c["id"] != p["id"]]
        if above_same:
            nearest = min(c["level"] for c in above_same)
            cands = [c for c in above_same if c["level"] == nearest]
            gap = "adjacent" if nearest - lvl <= 3 else "gap"
            if len(cands) == 1:
                c = cands[0]
                # does the title's team qualifier agree with the only candidate's?
                if p["quals"] and c["quals"]:
                    tq = "team-agrees" if p["quals"] & c["quals"] else "team-conflict"
                else:
                    tq = "no-team-info"
                add(p, "edge", f"single-leader-above:{gap}:{tq}:{src}:L{int(lvl)}", c,
                    note=f"Only {LEVEL_LABEL.get(nearest, nearest)}-level person in {fn}"
                         + (" (team names differ: possibly a missing manager)" if tq == "team-conflict" else ""))
                continue
            # several candidates at the nearest level -> team / location disambiguation
            team = [c for c in cands if jacc(p["quals"], c["quals"]) > 0]
            if len(team) == 1:
                add(p, "edge", f"team-match:{src}:L{int(lvl)}", team[0], cands,
                    note=f"Shares team '{', '.join(sorted(p['quals'] & team[0]['quals']))}' with the only matching manager")
                continue
            pool = team if team else cands
            if p["city"] and p["city"] != "remote":
                loc = [c for c in pool if c["city"] == p["city"]]
                if len(loc) == 1:
                    rule = "team+location" if team else ("location-only:qual" if p["quals"] else "location-only")
                    add(p, "edge", f"{rule}:{src}:L{int(lvl)}", loc[0], pool,
                        note=f"Same team and city ({p['city'].title()})" if team else
                        f"Only candidate manager in {p['city'].title()}")
                    continue
            cm = common_manager(pool)
            if cm:
                add(p, "rollup", f"rollup:{src}", {"id": cm}, pool,
                    note=f"{len(pool)} possible managers who all report to the same leader")
            else:
                add(p, "none", f"ambiguous:{src}", None, pool, note=f"{len(pool)} equally plausible managers")
            continue

        # p is the top of its function -> parent function leader, or the CEO
        # the first parent function that exists at this company decides; if its leader is not
        # more senior than p, p reports to the CEO instead
        parents = []
        for pf in PARENT_FUNCTIONS.get(fn, []):
            if by_fn.get(pf):
                top_lvl = max(c["level"] for c in by_fn[pf])
                parents = [c for c in by_fn[pf] if c["level"] == top_lvl and c["level"] > lvl and c["level"] >= 4]
                break
        if parents:
            if len(parents) == 1:
                add(p, "edge", f"subfunction->parent-leader:{fn}:L{int(lvl)}", parents[0],
                    note=f"Top {fn} person; {fn} usually sits under {parents[0]['function']}")
            elif ceo:
                add(p, "rollup", f"rollup:{src}", ceo, parents, note="Several possible parent leaders")
            else:
                add(p, "none", "no-unique-ceo", None, parents, note="Several possible parent leaders")
            continue
        if lvl < 4:
            add(p, "none", f"no-manager-tier-found:{fn}", note=f"No manager-level {fn} person found above this IC")
            continue
        if not ceo:
            add(p, "none", "no-unique-ceo", note="Top of its function, but no single CEO was identified")
            continue
        add(p, "edge", f"function-head->ceo:{fn}:L{int(lvl)}", ceo,
            note=f"Most senior {fn} person; no higher {fn} leader exists")

    return decisions


# ---------------------------------------------------------------------------
# 5. Pipeline
# ---------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "orgchart.db"))
    ap.add_argument("--min-precision", type=float, default=0.90,
                    help="only write edges from rules whose calibrated precision is at least this")
    ap.add_argument("--min-support", type=int, default=20, help="rules seen fewer times than this are rejected")
    args = ap.parse_args()

    con = sqlite3.connect(args.db)
    con.row_factory = sqlite3.Row
    cur = con.cursor()

    rows = cur.execute("SELECT * FROM employees ORDER BY company_id, id").fetchall()
    people_by_co = defaultdict(list)
    parsed = {}
    for r in rows:
        clean, toks = normalize_title(r["title"])
        lvl, lvl_reason = parse_level(clean, toks)
        fn, fn_src = parse_function(clean, r["department"])
        if lvl is not None and lvl >= 8 and fn is None:
            fn = "executive"
        if lvl == 9:
            fn = "executive"
        city = normalize_city(r["location"])
        p = dict(id=r["id"], company_id=r["company_id"], name=normalize_name(r["full_name"]), email=r["email"],
                 clean=clean, level=lvl, level_reason=lvl_reason, function=fn, fn_src=fn_src,
                 quals=qualifiers(clean, toks), city=city, region=city_region(city), dup_of=None,
                 title_present=bool(r["title"] and r["title"].strip()))
        parsed[p["id"]] = p
        people_by_co[r["company_id"]].append(p)

    # --- de-duplicate (same person scraped twice)
    n_dups = 0
    for cid, ppl in people_by_co.items():
        seen = {}
        for p in sorted(ppl, key=lambda x: (not x["title_present"], x["id"])):
            keys = [("n", p["name"])] + ([("e", p["email"])] if p["email"] else [])
            hit = next((seen[k] for k in keys if k in seen), None)
            if hit and (hit["function"] == p["function"] or not p["function"] or not hit["function"]):
                p["dup_of"] = hit["id"]
                n_dups += 1
                continue
            for k in keys:
                seen.setdefault(k, p)

    # --- resolve
    decisions = {}
    for cid, ppl in people_by_co.items():
        decisions.update(resolve_company([p for p in ppl if not p["dup_of"]]))

    # --- calibrate each rule against ground truth on a training split (even company ids)
    truth = {r[0]: (r[1], r[2]) for r in cur.execute(
        "SELECT employee_id, true_manager_id, duplicate_of FROM ground_truth")}
    true_mgr = {k: v[0] for k, v in truth.items()}

    def ancestors(eid):
        out, seen = [], set()
        m = true_mgr.get(eid)
        while m and m not in seen:
            out.append(m)
            seen.add(m)
            m = true_mgr.get(m)
        return out

    stats = defaultdict(lambda: {"train": [0, 0], "test": [0, 0]})
    for eid, d in decisions.items():
        if d["kind"] not in ("edge", "rollup"):
            continue
        split = "train" if parsed[eid]["company_id"] % 2 == 0 else "test"
        if d["kind"] == "edge":
            ok = true_mgr.get(eid) == d["target"]["id"]
        else:
            ok = d["target"]["id"] in ancestors(eid)
        s = stats[d["rule"]][split]
        s[0] += 1
        s[1] += int(ok)

    rule_conf = {}
    for rule, s in stats.items():
        n, k = s["train"]
        # Laplace-smoothed precision, shrunk towards 0.5 for small samples
        prec = (k + 1) / (n + 2)
        rule_conf[rule] = (prec, n)

    def accepted(rule):
        prec, n = rule_conf.get(rule, (0, 0))
        return n >= args.min_support and prec >= args.min_precision

    # --- write output tables
    cur.executescript("""
    DROP TABLE IF EXISTS people_inferred; DROP TABLE IF EXISTS reporting_edges; DROP TABLE IF EXISTS rollups;
    DROP TABLE IF EXISTS manager_candidates; DROP TABLE IF EXISTS inference_rules; DROP TABLE IF EXISTS metrics;
    CREATE TABLE people_inferred (
      employee_id INTEGER PRIMARY KEY, company_id INTEGER, clean_title TEXT, function TEXT, function_source TEXT,
      level REAL, level_label TEXT, level_reason TEXT, team_qualifiers TEXT, city TEXT, duplicate_of INTEGER,
      status TEXT  -- 'top' | 'resolved' | 'rollup' | 'unresolved' | 'duplicate'
    );
    CREATE TABLE reporting_edges (
      employee_id INTEGER PRIMARY KEY, manager_id INTEGER, company_id INTEGER, confidence REAL, rule TEXT, reason TEXT
    );
    CREATE TABLE rollups (
      employee_id INTEGER PRIMARY KEY, ancestor_id INTEGER, company_id INTEGER, confidence REAL, rule TEXT, reason TEXT
    );
    CREATE TABLE manager_candidates (employee_id INTEGER, candidate_id INTEGER, company_id INTEGER);
    CREATE TABLE inference_rules (
      rule TEXT PRIMARY KEY, kind TEXT, accepted INTEGER, train_n INTEGER, train_precision REAL,
      test_n INTEGER, test_precision REAL
    );
    CREATE TABLE metrics (key TEXT PRIMARY KEY, value REAL, label TEXT);
    """)

    status_counts = Counter()
    for eid, p in parsed.items():
        d = decisions.get(eid)
        status = "duplicate" if p["dup_of"] else None
        if d and not status:
            rule = d["rule"]
            if d["kind"] == "edge" and accepted(rule):
                status = "resolved"
                cur.execute("INSERT INTO reporting_edges VALUES (?,?,?,?,?,?)",
                            (eid, d["target"]["id"], p["company_id"], round(rule_conf[rule][0], 3), rule, d["note"]))
            elif d["kind"] in ("edge", "rollup"):
                # rejected direct edge -> keep only a rollup if one is certain enough
                target = d["target"]["id"]
                if d["kind"] == "rollup" and accepted(rule):
                    status = "rollup"
                    cur.execute("INSERT INTO rollups VALUES (?,?,?,?,?,?)",
                                (eid, target, p["company_id"], round(rule_conf[rule][0], 3), rule, d["note"]))
                else:
                    status = "unresolved"
                    # rejected edge: surface its target as a candidate, never as an edge
                    cands = d["cands"] or [d["target"]]
                    for c in cands:
                        if c.get("company_id") is not None or c["id"] in parsed:
                            cur.execute("INSERT INTO manager_candidates VALUES (?,?,?)", (eid, c["id"], p["company_id"]))
            elif rule.startswith("ceo:top"):
                status = "top"
            else:
                status = "unresolved"
                for c in d["cands"]:
                    cur.execute("INSERT INTO manager_candidates VALUES (?,?,?)", (eid, c["id"], p["company_id"]))
        status_counts[status] += 1
        cur.execute("INSERT INTO people_inferred VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                    (eid, p["company_id"], p["clean"], p["function"], p["fn_src"], p["level"],
                     LEVEL_LABEL.get(p["level"], str(p["level"])), p["level_reason"],
                     ", ".join(sorted(p["quals"])), p["city"], p["dup_of"], status))

    for rule, s in sorted(stats.items()):
        tn, tk = s["train"]
        vn, vk = s["test"]
        kind = "rollup" if rule.startswith("rollup") else "edge"
        cur.execute("INSERT INTO inference_rules VALUES (?,?,?,?,?,?,?)",
                    (rule, kind, int(accepted(rule)), tn, round(rule_conf[rule][0], 4), vn,
                     round(vk / vn, 4) if vn else None))

    # --- evaluation on the held-out (odd id) companies
    def evaluate(split_mod):
        real = [e for e, p in parsed.items() if p["company_id"] % 2 == split_mod and not truth[e][1]]
        has_mgr = [e for e in real if true_mgr.get(e)]
        edges = {r[0]: r[1] for r in cur.execute("SELECT employee_id, manager_id FROM reporting_edges")}
        ups = {r[0]: r[1] for r in cur.execute("SELECT employee_id, ancestor_id FROM rollups")}
        e_in = [e for e in has_mgr if e in edges]
        correct = sum(1 for e in e_in if edges[e] == true_mgr[e])
        r_in = [e for e in has_mgr if e in ups]
        r_ok = sum(1 for e in r_in if ups[e] in ancestors(e))
        dup_true = [e for e, p in parsed.items() if p["company_id"] % 2 == split_mod and truth[e][1]]
        dup_found = sum(1 for e in dup_true if parsed[e]["dup_of"] == truth[e][1]
                        or parsed[truth[e][1]]["dup_of"] == e)
        dup_flagged = [e for e, p in parsed.items() if p["company_id"] % 2 == split_mod and p["dup_of"]]
        dup_flag_ok = sum(1 for e in dup_flagged if truth[e][1] is not None or
                          truth.get(parsed[e]["dup_of"], (None, None))[1] == e)
        return dict(people=len(real), with_manager=len(has_mgr), edges=len(e_in), correct=correct,
                    precision=correct / len(e_in) if e_in else 0, coverage=len(e_in) / len(has_mgr),
                    rollups=len(r_in), rollup_precision=r_ok / len(r_in) if r_in else 0,
                    placed=(len(e_in) + len(r_in)) / len(has_mgr),
                    dup_recall=dup_found / len(dup_true) if dup_true else 0,
                    dup_precision=dup_flag_ok / len(dup_flagged) if dup_flagged else 0)

    test = evaluate(1)
    train = evaluate(0)
    metrics = [
        ("test_precision", test["precision"], "Edge precision (held-out companies)"),
        ("test_coverage", test["coverage"], "Share of people with a direct manager edge"),
        ("test_rollup_precision", test["rollup_precision"], "Roll-up precision (held-out companies)"),
        ("test_placed", test["placed"], "Share of people placed by an edge or a roll-up"),
        ("test_edges", test["edges"], "Edges on held-out companies"),
        ("test_wrong_edges", test["edges"] - test["correct"], "Wrong edges on held-out companies"),
        ("train_precision", train["precision"], "Edge precision (calibration companies)"),
        ("train_coverage", train["coverage"], "Coverage (calibration companies)"),
        ("dup_recall", test["dup_recall"], "Duplicate rows caught"),
        ("dup_precision", test["dup_precision"], "Duplicate flags that were right"),
        ("min_precision", args.min_precision, "Precision bar a rule must clear to emit edges"),
        ("companies", len(people_by_co), "Companies"),
        ("employees", len(parsed), "Employee rows"),
    ]
    cur.executemany("INSERT INTO metrics VALUES (?,?,?)", metrics)
    cur.executescript("""
      CREATE INDEX idx_edges_mgr ON reporting_edges(manager_id);
      CREATE INDEX idx_edges_co ON reporting_edges(company_id);
      CREATE INDEX idx_pi_co ON people_inferred(company_id);
      CREATE INDEX idx_roll_co ON rollups(company_id);
      CREATE INDEX idx_cand_emp ON manager_candidates(employee_id);
    """)
    con.commit()
    con.execute("VACUUM")
    con.close()

    print(f"duplicates flagged: {n_dups}")
    print("status:", dict(status_counts))
    for k, v, label in metrics:
        print(f"  {label:50s} {v:.3f}" if isinstance(v, float) else f"  {label:50s} {v}")


if __name__ == "__main__":
    main()
