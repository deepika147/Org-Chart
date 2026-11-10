#!/usr/bin/env python3
"""
Generate a messy synthetic "flat employee list" dataset into SQLite.

For every company we first build a *true* reporting hierarchy, then we throw the
hierarchy away and keep only what a data vendor would give you: a flat list of
people with (messy) job titles, departments and locations. The true manager is
kept in a separate `ground_truth` table so the inference can be scored.

Usage:
    pip install -r requirements.txt
    python generate_data.py                  # writes data/orgchart.db
    python generate_data.py --companies 520 --seed 42 --out data/orgchart.db
"""
import argparse
import math
import os
import random
import re
import sqlite3
from datetime import date, timedelta

from faker import Faker

# --------------------------------------------------------------------------
# Function catalogue: what a "function" looks like inside a 100-150 person co.
# --------------------------------------------------------------------------
FUNCTIONS = {
    "engineering": dict(
        noun="Engineering",
        depts=["Engineering", "Eng", "R&D", "Technology", "Product & Engineering", "Tech", "Software Development"],
        exec=["Chief Technology Officer", "CTO"],
        vp=["VP of Engineering", "Vice President, Engineering", "VP Engineering"],
        head=["Head of Engineering"],
        director=["Director of Engineering", "Engineering Director"],
        mgr=["Engineering Manager", "Software Engineering Manager", "Manager, Software Engineering"],
        areas=["Platform", "Payments", "Mobile", "Infrastructure", "Frontend", "Backend",
               "Integrations", "Core Services", "API", "Checkout", "Identity"],
        ics=[("Software Engineer", 30), ("Senior Software Engineer", 26), ("Staff Software Engineer", 5),
             ("Principal Engineer", 2), ("Junior Software Engineer", 5), ("QA Engineer", 6),
             ("Site Reliability Engineer", 4), ("DevOps Engineer", 4), ("Frontend Engineer", 6),
             ("Backend Engineer", 6), ("Software Engineer II", 5), ("Lead Software Engineer", 3),
             ("Software Engineering Intern", 2), ("Mobile Engineer", 4), ("Full Stack Developer", 5)],
    ),
    "data": dict(
        noun="Data",
        depts=["Data", "Analytics", "Data & Analytics", "Engineering", "BI"],
        exec=["Chief Data Officer"],
        vp=["VP of Data"],
        head=["Head of Data", "Head of Data & Analytics"],
        director=["Director of Data", "Director of Analytics"],
        mgr=["Data Science Manager", "Analytics Manager", "Data Engineering Manager"],
        areas=["Data Engineering", "Analytics", "Machine Learning", "Data Science", "BI"],
        ics=[("Data Engineer", 20), ("Senior Data Engineer", 10), ("Data Analyst", 20),
             ("Senior Data Analyst", 8), ("Data Scientist", 14), ("Senior Data Scientist", 6),
             ("Machine Learning Engineer", 8), ("Analytics Engineer", 6), ("BI Developer", 4)],
    ),
    "security": dict(
        noun="Security",
        depts=["Security", "InfoSec", "Information Security", "Engineering", "IT"],
        exec=["Chief Information Security Officer", "CISO"],
        vp=["VP of Security"],
        head=["Head of Security", "Head of Information Security"],
        director=["Director of Security", "Security Director"],
        mgr=["Security Manager", "Security Engineering Manager"],
        areas=["AppSec", "Security Operations", "GRC", "Cloud Security"],
        ics=[("Security Engineer", 30), ("Senior Security Engineer", 15), ("Security Analyst", 20),
             ("GRC Analyst", 8), ("Application Security Engineer", 8)],
    ),
    "it": dict(
        noun="IT",
        depts=["IT", "Information Technology", "Operations", "Tech Ops"],
        exec=["Chief Information Officer", "CIO"],
        vp=["VP of IT"],
        head=["Head of IT"],
        director=["Director of IT", "IT Director"],
        mgr=["IT Manager"],
        areas=["Helpdesk", "Corporate IT", "Systems"],
        ics=[("IT Support Specialist", 30), ("Systems Administrator", 20), ("IT Analyst", 15),
             ("Help Desk Technician", 15), ("Network Engineer", 8)],
    ),
    "product": dict(
        noun="Product",
        depts=["Product", "Product Management", "Product & Engineering", "R&D"],
        exec=["Chief Product Officer", "CPO"],
        vp=["VP of Product", "Vice President of Product"],
        head=["Head of Product"],
        director=["Director of Product", "Director of Product Management"],
        mgr=["Group Product Manager"],
        areas=["Platform", "Payments", "Mobile", "Growth", "Core", "Integrations"],
        ics=[("Product Manager", 40), ("Senior Product Manager", 25), ("Associate Product Manager", 10),
             ("Technical Product Manager", 10), ("Product Owner", 8), ("Product Analyst", 7)],
    ),
    "design": dict(
        noun="Design",
        depts=["Design", "UX", "Product", "Product Design", "Creative"],
        exec=["Chief Design Officer"],
        vp=["VP of Design"],
        head=["Head of Design", "Head of UX"],
        director=["Director of Design", "Design Director"],
        mgr=["Design Manager", "UX Manager"],
        areas=["Product Design", "UX Research", "Brand Design"],
        ics=[("Product Designer", 35), ("Senior Product Designer", 20), ("UX Designer", 15),
             ("UX Researcher", 12), ("UI Designer", 10), ("Visual Designer", 8)],
    ),
    "sales": dict(
        noun="Sales",
        depts=["Sales", "GTM", "Revenue", "Go-To-Market", "Business Development", "Commercial"],
        exec=["Chief Revenue Officer", "CRO", "Chief Sales Officer"],
        vp=["VP of Sales", "Vice President of Sales", "VP Sales"],
        head=["Head of Sales"],
        director=["Sales Director", "Director of Sales", "Regional Sales Director"],
        mgr=["Sales Manager", "Regional Sales Manager", "Manager, Sales"],
        areas=["Enterprise", "Mid-Market", "SMB", "EMEA", "APAC", "North America", "Strategic Accounts"],
        ics=[("Account Executive", 30), ("Senior Account Executive", 15), ("Sales Development Representative", 18),
             ("Business Development Representative", 10), ("Account Manager", 10),
             ("Solutions Engineer", 7), ("Sales Engineer", 5), ("Enterprise Account Executive", 7)],
    ),
    "marketing": dict(
        noun="Marketing",
        depts=["Marketing", "Mktg", "Growth", "GTM", "Brand"],
        exec=["Chief Marketing Officer", "CMO"],
        vp=["VP of Marketing", "Vice President, Marketing", "VP Marketing"],
        head=["Head of Marketing"],
        director=["Director of Marketing", "Marketing Director"],
        mgr=["Senior Manager, Marketing", "Marketing Team Lead"],
        areas=["Growth", "Product Marketing", "Brand", "Content", "Demand Generation", "Events"],
        ics=[("Marketing Manager", 20), ("Marketing Specialist", 18), ("Content Writer", 10),
             ("Product Marketing Manager", 12), ("Growth Marketer", 10), ("SEO Specialist", 8),
             ("Marketing Coordinator", 10), ("Social Media Manager", 6), ("Graphic Designer", 6)],
    ),
    "customer_success": dict(
        noun="Customer Success",
        depts=["Customer Success", "CS", "Customer Experience", "Client Services", "Account Management"],
        exec=["Chief Customer Officer"],
        vp=["VP of Customer Success", "VP Customer Success"],
        head=["Head of Customer Success"],
        director=["Director of Customer Success", "Customer Success Director"],
        mgr=["Customer Success Team Lead", "Manager, Customer Success"],
        areas=["Enterprise", "SMB", "Onboarding", "Renewals", "EMEA"],
        ics=[("Customer Success Manager", 45), ("Senior Customer Success Manager", 20),
             ("Onboarding Specialist", 15), ("Implementation Consultant", 12), ("Renewals Specialist", 8)],
    ),
    "support": dict(
        noun="Support",
        depts=["Support", "Customer Support", "Customer Service", "Customer Experience", "CX"],
        exec=["Chief Customer Officer"],
        vp=["VP of Support"],
        head=["Head of Support", "Head of Customer Support"],
        director=["Director of Support", "Director of Customer Support"],
        mgr=["Support Manager", "Support Team Lead", "Customer Support Manager"],
        areas=["Tier 1", "Tier 2", "Technical Support", "EMEA", "APAC"],
        ics=[("Customer Support Specialist", 35), ("Support Engineer", 20), ("Technical Support Engineer", 15),
             ("Customer Support Representative", 20), ("Senior Support Engineer", 10)],
    ),
    "finance": dict(
        noun="Finance",
        depts=["Finance", "Accounting", "Finance & Accounting", "G&A", "F&A"],
        exec=["Chief Financial Officer", "CFO"],
        vp=["VP of Finance", "VP Finance"],
        head=["Head of Finance"],
        director=["Director of Finance", "Finance Director", "Controller"],
        mgr=["Accounting Manager", "FP&A Manager", "Finance Manager"],
        areas=["FP&A", "Accounting", "Payroll", "Tax", "Revenue Operations"],
        ics=[("Accountant", 25), ("Senior Accountant", 15), ("Financial Analyst", 25),
             ("Staff Accountant", 10), ("Accounts Payable Specialist", 10), ("Payroll Specialist", 8),
             ("Billing Specialist", 7)],
    ),
    "people": dict(
        noun="People",
        depts=["People", "HR", "Human Resources", "People Ops", "Talent", "G&A"],
        exec=["Chief People Officer", "Chief Human Resources Officer", "CHRO"],
        vp=["VP of People", "VP Human Resources"],
        head=["Head of People", "Head of HR", "Head of Talent"],
        director=["HR Director", "Director of People", "Director of Talent Acquisition"],
        mgr=["HR Manager", "Talent Acquisition Manager", "People Operations Manager"],
        areas=["Talent Acquisition", "People Operations", "Total Rewards", "L&D"],
        ics=[("Recruiter", 30), ("Senior Recruiter", 10), ("HR Generalist", 20), ("HR Business Partner", 15),
             ("People Operations Specialist", 10), ("Recruiting Coordinator", 10), ("HR Coordinator", 5)],
    ),
    "legal": dict(
        noun="Legal",
        depts=["Legal", "Legal & Compliance", "G&A", "Compliance"],
        exec=["General Counsel", "Chief Legal Officer"],
        vp=["VP of Legal", "VP, Legal"],
        head=["Head of Legal"],
        director=["Director of Legal", "Deputy General Counsel"],
        mgr=["Legal Manager", "Compliance Manager"],
        areas=["Commercial", "Compliance", "Privacy"],
        ics=[("Corporate Counsel", 30), ("Paralegal", 30), ("Legal Counsel", 20), ("Compliance Analyst", 15),
             ("Contracts Manager", 5)],
    ),
    "operations": dict(
        noun="Operations",
        depts=["Operations", "Ops", "Business Operations", "G&A", "Strategy & Ops"],
        exec=["Chief Operating Officer", "COO"],
        vp=["VP of Operations", "VP Operations"],
        head=["Head of Operations", "Head of Business Operations"],
        director=["Director of Operations", "Operations Director"],
        mgr=["Operations Manager", "Business Operations Manager"],
        areas=["Business Operations", "Supply Chain", "Procurement", "Facilities", "Logistics"],
        ics=[("Operations Analyst", 25), ("Operations Associate", 20), ("Office Manager", 8),
             ("Project Manager", 12), ("Program Manager", 10), ("Procurement Specialist", 10),
             ("Operations Coordinator", 15)],
    ),
}

# headcount weight per function, by industry (remaining go to engineering)
INDUSTRIES = {
    "B2B SaaS":        dict(engineering=28, data=4, security=2, it=2, product=6, design=3, sales=17, marketing=8, customer_success=8, support=6, finance=4, people=4, legal=2, operations=4),
    "Fintech":         dict(engineering=26, data=5, security=4, it=2, product=6, design=3, sales=12, marketing=6, customer_success=5, support=8, finance=7, people=4, legal=5, operations=5),
    "Healthcare Tech": dict(engineering=22, data=6, security=3, it=3, product=5, design=3, sales=13, marketing=6, customer_success=9, support=8, finance=5, people=4, legal=4, operations=8),
    "E-commerce":      dict(engineering=22, data=5, security=2, it=2, product=6, design=4, sales=6, marketing=15, customer_success=3, support=13, finance=5, people=4, legal=2, operations=11),
    "Cybersecurity":   dict(engineering=28, data=3, security=9, it=2, product=6, design=2, sales=15, marketing=7, customer_success=6, support=7, finance=4, people=4, legal=2, operations=3),
    "EdTech":          dict(engineering=22, data=4, security=2, it=2, product=7, design=5, sales=12, marketing=12, customer_success=8, support=10, finance=4, people=4, legal=2, operations=6),
    "Logistics":       dict(engineering=18, data=5, security=2, it=4, product=5, design=2, sales=12, marketing=5, customer_success=6, support=10, finance=6, people=5, legal=2, operations=18),
    "Manufacturing":   dict(engineering=16, data=3, security=2, it=5, product=5, design=2, sales=14, marketing=5, customer_success=4, support=7, finance=7, people=6, legal=3, operations=21),
    "Media":           dict(engineering=18, data=6, security=2, it=3, product=6, design=8, sales=15, marketing=15, customer_success=4, support=5, finance=5, people=5, legal=4, operations=4),
    "AI / ML":         dict(engineering=30, data=14, security=3, it=2, product=7, design=3, sales=12, marketing=6, customer_success=5, support=4, finance=4, people=4, legal=2, operations=4),
    "Real Estate Tech":dict(engineering=20, data=4, security=2, it=3, product=6, design=4, sales=18, marketing=10, customer_success=7, support=8, finance=6, people=4, legal=4, operations=8),
    "Consulting":      dict(engineering=12, data=8, security=3, it=4, product=3, design=4, sales=18, marketing=6, customer_success=14, support=3, finance=7, people=8, legal=3, operations=10),
}

# sub-functions that usually sit under another function's leader
PARENT_FUNCTION = {
    "data": "engineering",
    "security": "engineering",
    "it": "operations",
    "design": "product",
    "support": "customer_success",
}

OFFICES_BY_COUNTRY = {
    "US": ["San Francisco", "New York", "Austin", "Seattle", "Boston", "Chicago", "Denver", "Atlanta"],
    "UK": ["London", "Manchester", "Edinburgh"],
    "IN": ["Bengaluru", "Hyderabad", "Pune", "Gurugram", "Chennai", "Mumbai"],
    "DE": ["Berlin", "Munich", "Hamburg"],
    "FR": ["Paris", "Lyon"],
    "SG": ["Singapore"],
    "AU": ["Sydney", "Melbourne"],
    "CA": ["Toronto", "Vancouver"],
    "NL": ["Amsterdam"],
    "ES": ["Madrid", "Barcelona"],
}
REGION_OF_CITY = {}
for _c, _cities in OFFICES_BY_COUNTRY.items():
    for _city in _cities:
        REGION_OF_CITY[_city] = {"US": "North America", "CA": "North America", "IN": "APAC", "SG": "APAC",
                                 "AU": "APAC"}.get(_c, "EMEA")

# messy spellings of the same city
CITY_VARIANTS = {
    "San Francisco": ["San Francisco, CA", "SF", "San Francisco Bay Area", "san francisco"],
    "New York": ["New York, NY", "NYC", "New York City", "Greater New York Area"],
    "Bengaluru": ["Bangalore", "Bengaluru, Karnataka", "Bangalore Urban"],
    "Gurugram": ["Gurgaon", "Gurugram, Haryana", "Delhi NCR"],
    "London": ["London, UK", "Greater London", "London, England"],
    "Munich": ["München", "Munich, Germany"],
    "Mumbai": ["Bombay", "Mumbai, Maharashtra"],
    "Seattle": ["Seattle, WA", "Greater Seattle Area"],
    "Austin": ["Austin, TX"],
    "Boston": ["Boston, MA", "Greater Boston"],
}

# title noise ---------------------------------------------------------------
ABBREV = [
    ("Senior ", ["Sr. ", "Sr ", "Snr ", "Senior "]),
    ("Vice President", ["VP", "V.P.", "Vice Pres."]),
    ("Director", ["Dir.", "Dir", "Director"]),
    ("Manager", ["Mgr", "Mgr.", "Manager"]),
    ("Engineering", ["Eng", "Engg", "Engineering"]),
    ("Engineer", ["Engr", "Eng.", "Engineer"]),
    ("Software Engineer", ["SWE", "Software Engineer", "Software Dev Engineer"]),
    ("Operations", ["Ops", "Operations"]),
    ("Development", ["Dev", "Development"]),
    ("Marketing", ["Mktg", "Marketing"]),
    ("Customer Success", ["CS", "Customer Success"]),
    ("Representative", ["Rep", "Representative"]),
    ("Account Executive", ["AE", "Account Executive"]),
    ("Human Resources", ["HR"]),
    ("Information Security", ["InfoSec"]),
    ("Associate", ["Assoc.", "Associate"]),
    ("Junior ", ["Jr. ", "Jr ", "Junior "]),
    ("Technology", ["Tech"]),
    ("Chief Executive Officer", ["CEO"]),
    ("and", ["&"]),
]
TYPOS = {
    "Engineer": ["Enginer", "Engineeer", "Enigneer"],
    "Manager": ["Manger", "Mananger", "Managr"],
    "Director": ["Directer", "Diretor"],
    "Marketing": ["Marketting", "Markting"],
    "Senior": ["Senor", "Seinor"],
    "Analyst": ["Analist", "Anaylst"],
    "Product": ["Prodcut"],
    "Specialist": ["Specalist", "Specialst"],
    "Recruiter": ["Recuiter"],
}
NOISE_SUFFIX = [" | We're hiring!", " (Remote)", " 🚀", " - Open to opportunities", " | Ex-Google", ", MBA",
                " | Building the future of work", " ✨", " (contract)", " | Ex-Amazon", ", PhD", ", CPA"]
FOREIGN = {
    "Software Engineer": ["Ingeniero de Software", "Softwareentwickler", "Ingénieur logiciel"],
    "Sales Manager": ["Gerente de Ventas", "Vertriebsleiter", "Responsable Commercial"],
    "Account Executive": ["Ejecutivo de Cuentas", "Chargé d'affaires"],
    "Product Manager": ["Chef de produit", "Produktmanager"],
    "Head of Sales": ["Directeur Commercial", "Leiter Vertrieb"],
    "Data Analyst": ["Analista de Datos", "Datenanalyst"],
    "Recruiter": ["Reclutador", "Recruteur"],
    "Accountant": ["Contador", "Comptable", "Buchhalter"],
}
VAGUE = ["Manager", "Team Member", "Employee", "Consultant", "Staff", "Associate", "Member of Technical Staff",
         "Specialist", "Individual Contributor", "--", "N/A", "Analyst"]


class Gen:
    def __init__(self, seed):
        self.rng = random.Random(seed)
        self.fake = Faker(["en_US", "en_GB", "en_IN", "de_DE", "es_ES", "fr_FR"])
        Faker.seed(seed)
        self.next_emp_id = 1
        self.used_company_names = set()

    # ------------------------------------------------------------------ utils
    def pick(self, seq):
        return self.rng.choice(seq)

    def wpick(self, pairs):
        items, weights = zip(*pairs)
        return self.rng.choices(items, weights=weights, k=1)[0]

    def company_name(self):
        while True:
            base = self.fake["en_US"].company().replace(",", "")
            style = self.rng.random()
            if style < 0.25:
                base = base.split()[0] + self.pick(["ly", "ify", "io", "hub", "labs", "base", "stack"])
            elif style < 0.4:
                base = base.split()[0] + " " + self.pick(["Labs", "AI", "Technologies", "Systems", "Health", "Pay", "Cloud"])
            if base not in self.used_company_names:
                self.used_company_names.add(base)
                return base

    # ------------------------------------------------------------ messiness
    def messy_title(self, title, function, team=None, region=None):
        r = self.rng
        if r.random() < 0.015:
            return None                       # missing title
        if r.random() < 0.02:
            return self.pick(VAGUE)            # useless title
        if title in FOREIGN and r.random() < 0.12:
            t = self.pick(FOREIGN[title])
            if team and r.random() < 0.3:
                t += f" - {team}"
            return t
        t = title
        # attach team / region qualifier in one of several styles
        q = team
        if region and r.random() < 0.25:
            q = region if not q else f"{q}, {region}"
        if q:
            style = r.random()
            if style < 0.4:
                t = f"{t}, {q}"
            elif style < 0.6:
                t = f"{t} - {q}"
            elif style < 0.72:
                t = f"{t} ({q})"
            elif style < 0.82:
                t = f"{t} | {q}"
            elif style < 0.9 and t.startswith(("Director of ", "Head of ")):
                t = f"{t} {q}"
            else:
                t = f"{t}, {q}"
        # abbreviations
        for full, variants in ABBREV:
            pat = r"\b" + re.escape(full.strip()) + r"\b"
            if re.search(pat, t) and r.random() < 0.35:
                rep = self.pick(variants).strip()
                t = re.sub(pat, lambda _m: rep, t, count=1)
        # typos
        if r.random() < 0.04:
            for word, typos in TYPOS.items():
                if word in t:
                    t = t.replace(word, self.pick(typos), 1)
                    break
        # prefixes / suffixes
        if r.random() < 0.02:
            t = self.pick(["Interim ", "Acting ", "Founding "]) + t
        if r.random() < 0.05:
            t += self.pick(NOISE_SUFFIX)
        # casing
        c = r.random()
        if c < 0.04:
            t = t.upper()
        elif c < 0.09:
            t = t.lower()
        # whitespace junk
        if r.random() < 0.03:
            t = "  " + t.replace(" ", "  ", 1) + " "
        return t

    def messy_dept(self, function):
        r = self.rng
        if r.random() < 0.28:
            return None
        d = self.pick(FUNCTIONS[function]["depts"] if function in FUNCTIONS else
                      ["Executive", "Leadership", "Office of the CEO", "Management", "G&A"])
        if r.random() < 0.05:
            d = d.upper()
        elif r.random() < 0.05:
            d = d.lower()
        return d

    def messy_location(self, city):
        r = self.rng
        if city is None or r.random() < 0.14:
            return None
        if city == "Remote":
            return self.pick(["Remote", "Remote - US", "Remote (Global)", "Anywhere", "WFH"])
        if city in CITY_VARIANTS and r.random() < 0.45:
            return self.pick(CITY_VARIANTS[city])
        return city

    # ----------------------------------------------------------- hierarchy
    def new_person(self, company, title, function, level, manager, team=None, city=None, region=None,
                   tenure_bias=0.0):
        pid = self.next_emp_id
        self.next_emp_id += 1
        locale = self.pick(company["locales"])
        f = self.fake[locale]
        first, last = f.first_name(), f.last_name()
        start = company["founded"] + timedelta(days=int(self.rng.random() * (1 - tenure_bias) *
                                                         (date(2026, 10, 1) - company["founded"]).days))
        p = dict(id=pid, company_id=company["id"], first=first, last=last, true_title=title,
                 function=function, level=level, manager=manager, team=team, city=city, region=region,
                 start=start, dup_of=None)
        company["people"].append(p)
        return p

    def pick_city(self, company, near=None, stickiness=0.75):
        if near and self.rng.random() < stickiness:
            return near
        if self.rng.random() < 0.08:
            return "Remote"
        return self.pick(company["offices"])

    def build_function(self, company, fn, headcount, parent, top_kind):
        """Build one function's subtree under `parent`. Returns the function leader."""
        spec = FUNCTIONS[fn]
        r = self.rng
        # leader
        title = self.pick(spec[top_kind])
        lvl = {"exec": 8, "vp": 7, "head": 6, "director": 5, "mgr": 4}[top_kind]
        leader = self.new_person(company, title, fn, lvl, parent, city=company["hq"], tenure_bias=0.6)
        remaining = headcount - 1
        if remaining <= 0:
            return leader

        top = leader
        # optional VP under an exec when the function is big enough
        if top_kind == "exec" and remaining > 22 and r.random() < 0.45:
            top = self.new_person(company, self.pick(spec["vp"]), fn, 7, leader, city=company["hq"], tenure_bias=0.5)
            remaining -= 1

        n_ics_guess = remaining
        span = r.randint(5, 8)
        n_mgrs = 0 if n_ics_guess <= 7 else max(1, round(n_ics_guess / (span + 1)))
        if n_mgrs == 1 and r.random() < 0.5:
            n_mgrs = 0
        n_dirs = 0
        if n_mgrs >= 4 and r.random() < 0.6:
            n_dirs = max(2, round(n_mgrs / 3))

        areas = spec["areas"][:]
        r.shuffle(areas)

        # directors
        directors = []
        for i in range(n_dirs):
            area = areas[i % len(areas)]
            region = area if area in ("EMEA", "APAC", "North America") else None
            city = self._city_for_region(company, region) if region else self.pick_city(company, company["hq"], 0.6)
            d = self.new_person(company, self.pick(spec["director"]), fn, 5, top, team=area, city=city,
                                region=region, tenure_bias=0.4)
            directors.append(d)
        remaining -= n_dirs

        # managers
        managers = []
        for i in range(n_mgrs):
            if directors:
                boss = directors[i % len(directors)]
                # usually same area as the director, sometimes a sub-area
                area = boss["team"] if r.random() < 0.7 else areas[(n_dirs + i) % len(areas)]
                city = self.pick_city(company, boss["city"], 0.6)
            else:
                boss = top
                area = areas[i % len(areas)]
                city = self.pick_city(company, company["hq"], 0.55)
            region = area if area in ("EMEA", "APAC", "North America") else None
            if region:
                city = self._city_for_region(company, region) or city
            m = self.new_person(company, self.pick(spec["mgr"]), fn, 4, boss, team=area, city=city,
                                region=region, tenure_bias=0.25)
            managers.append(m)
        remaining -= n_mgrs

        # ICs
        bosses = managers or directors or [top]
        for i in range(max(0, remaining)):
            boss = bosses[i % len(bosses)] if r.random() < 0.8 else self.pick(bosses)
            team = boss.get("team")
            city = self.pick_city(company, boss["city"], 0.75)
            title = self.wpick(spec["ics"])
            self.new_person(company, title, fn, 2 if ("Senior" in title or "Staff" in title or "Lead" in title
                                                       or "Principal" in title) else 1,
                            boss, team=team if r.random() < 0.85 else None, city=city, region=boss.get("region"))
        return leader

    def _city_for_region(self, company, region):
        cands = [c for c in company["offices"] if REGION_OF_CITY.get(c) == region]
        if cands:
            return self.pick(cands)
        # company has no office there -> pick a canonical city in that region
        return self.pick({"EMEA": ["London", "Berlin", "Amsterdam"], "APAC": ["Singapore", "Sydney", "Bengaluru"],
                          "North America": ["New York", "Toronto", "Austin"]}[region])

    def build_company(self, cid):
        r = self.rng
        industry = self.pick(list(INDUSTRIES))
        country = self.wpick([("US", 45), ("UK", 10), ("IN", 18), ("DE", 6), ("FR", 4), ("SG", 4), ("AU", 4),
                              ("CA", 4), ("NL", 3), ("ES", 2)])
        hq = self.pick(OFFICES_BY_COUNTRY[country])
        offices = [hq]
        for _ in range(r.choice([0, 0, 1, 1, 2, 3])):
            c2 = self.pick(self.pick(list(OFFICES_BY_COUNTRY.values())))
            if c2 not in offices:
                offices.append(c2)
        locale_by_country = {"US": "en_US", "UK": "en_GB", "IN": "en_IN", "DE": "de_DE", "FR": "fr_FR",
                             "ES": "es_ES", "SG": "en_US", "AU": "en_GB", "CA": "en_US", "NL": "en_US"}
        locales = [locale_by_country[country]] * 4 + ["en_US", "en_IN"]
        founded = date(r.randint(2004, 2022), r.randint(1, 12), r.randint(1, 28))
        name = self.company_name()
        domain = "".join(ch for ch in name.lower() if ch.isalnum())[:18] + self.pick([".com", ".io", ".ai", ".co"])
        size = r.randint(100, 150)
        company = dict(id=cid, name=name, domain=domain, industry=industry, country=country, hq=hq,
                       offices=offices, founded=founded, locales=locales, people=[], size=size)

        # --- executive layer
        ceo = self.new_person(company, self.pick(["Chief Executive Officer", "CEO", "Co-Founder & CEO",
                                                  "Founder and CEO", "CEO & Co-founder", "President & CEO"]),
                              "executive", 9, None, city=hq, tenure_bias=1.0)
        if r.random() < 0.6:
            self.new_person(company, self.pick(["Executive Assistant to the CEO", "Executive Assistant",
                                                "EA to CEO"]), "executive", 1, ceo, city=hq)
        if r.random() < 0.3:
            self.new_person(company, self.pick(["Chief of Staff", "Chief of Staff to the CEO"]), "executive", 6,
                            ceo, city=hq, tenure_bias=0.3)

        # --- headcount per function
        weights = dict(INDUSTRIES[industry])
        for fn in list(weights):
            weights[fn] *= r.uniform(0.6, 1.4)
            if fn in ("legal", "it", "security", "design", "data") and r.random() < 0.2:
                weights[fn] = 0  # function doesn't exist at this company
        budget = size - len(company["people"])
        total_w = sum(weights.values())
        heads = {fn: int(budget * w / total_w) for fn, w in weights.items()}
        heads = {fn: h for fn, h in heads.items() if h > 0}
        short = budget - sum(heads.values())
        heads["engineering"] = heads.get("engineering", 0) + short
        # a function of 1 person is folded into its parent / engineering
        for fn in list(heads):
            if heads[fn] == 1 and fn != "engineering":
                tgt = PARENT_FUNCTION.get(fn, "engineering")
                if tgt not in heads:
                    tgt = "engineering"
                heads[tgt] += 1
                del heads[fn]

        # --- choose the kind of leader for each function
        def top_kind_for(fn, h):
            if fn in PARENT_FUNCTION:
                return r.choices(["head", "director", "mgr"], [4, 3, 3 if h < 8 else 0])[0]
            p_exec = {"engineering": 0.85, "finance": 0.7, "legal": 0.6, "sales": 0.4, "operations": 0.45,
                      "marketing": 0.35, "product": 0.35, "people": 0.25, "customer_success": 0.15}.get(fn, 0.1)
            if r.random() < p_exec:
                return "exec"
            if h >= 10:
                return r.choice(["vp", "vp", "head", "director"])
            return r.choice(["head", "director", "director", "mgr"])

        leaders = {}
        order = [fn for fn in ["engineering", "product", "operations", "sales", "marketing", "finance", "people",
                               "legal", "customer_success"] if fn in heads]
        for fn in order:
            leaders[fn] = self.build_function(company, fn, heads[fn], ceo, top_kind_for(fn, heads[fn]))

        # sub-functions (and occasionally different wiring)
        for fn in ["data", "security", "design", "it", "support"]:
            if fn not in heads:
                continue
            parent_fn = PARENT_FUNCTION[fn]
            kind = top_kind_for(fn, heads[fn])
            parent = leaders.get(parent_fn)
            if fn == "it" and parent is None:
                parent = leaders.get("finance")
            if fn == "support" and parent is None:
                parent = leaders.get("operations")
            # a sub-function only sits under a parent leader who is more senior than its own leader
            own_level = {"exec": 8, "vp": 7, "head": 6, "director": 5, "mgr": 4}[kind]
            if parent is not None and parent["level"] <= own_level:
                parent = None
            if parent is None or r.random() < 0.18:
                parent = ceo
                if r.random() < 0.6:
                    kind = "exec" if fn in ("security", "data", "it") else "vp"
            leaders[fn] = self.build_function(company, fn, heads[fn], parent, kind)

        # realistic re-wiring that titles alone can't reveal
        cro = leaders.get("sales")
        if "marketing" in leaders and cro and cro["level"] == 8 and r.random() < 0.3:
            leaders["marketing"]["manager"] = cro      # marketing under the CRO
        coo = leaders.get("operations")
        if "customer_success" in leaders and coo and coo["level"] == 8 and r.random() < 0.3:
            leaders["customer_success"]["manager"] = coo

        # --- duplicates: same person scraped twice with a different title format
        dups = []
        for p in company["people"]:
            if r.random() < 0.006:
                d = dict(p)
                d["id"] = self.next_emp_id
                self.next_emp_id += 1
                d["dup_of"] = p["id"]
                dups.append(d)
        company["people"].extend(dups)
        return company


def write_db(path, companies):
    if os.path.exists(path):
        os.remove(path)
    con = sqlite3.connect(path)
    cur = con.cursor()
    cur.executescript(open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "schema.sql")).read())
    g_rng = random.Random(7)
    for c in companies:
        cur.execute("INSERT INTO companies(id,name,domain,industry,country,hq_city,founded_year,employee_count) "
                    "VALUES (?,?,?,?,?,?,?,?)",
                    (c["id"], c["name"], c["domain"], c["industry"], c["country"], c["hq"], c["founded"].year,
                     len([p for p in c["people"] if not p["dup_of"]])))
        for p in c["people"]:
            # email: firstname.lastname@ / f.lastname@ / missing
            em = None
            x = g_rng.random()
            norm = lambda s: "".join(ch for ch in s.lower() if ch.isalpha())
            if x < 0.55:
                em = f"{norm(p['first'])}.{norm(p['last'])}@{c['domain']}"
            elif x < 0.75:
                em = f"{norm(p['first'])[:1]}{norm(p['last'])}@{c['domain']}"
            name = f"{p['first']} {p['last']}"
            if g_rng.random() < 0.03:
                name = name.upper()
            elif g_rng.random() < 0.03:
                name = f"{p['last']}, {p['first']}"
            start = p["start"].isoformat() if g_rng.random() > 0.2 else None
            cur.execute("INSERT INTO employees(id,company_id,full_name,title,department,location,email,start_date) "
                        "VALUES (?,?,?,?,?,?,?,?)",
                        (p["id"], c["id"], name, p["raw_title"], p["raw_dept"], p["raw_loc"], em, start))
            cur.execute("INSERT INTO ground_truth(employee_id,true_manager_id,true_function,true_level,true_title,"
                        "true_team,duplicate_of) VALUES (?,?,?,?,?,?,?)",
                        (p["id"], p["manager"]["id"] if p["manager"] else None, p["function"], p["level"],
                         p["true_title"], p["team"], p["dup_of"]))
    con.commit()
    con.execute("VACUUM")
    con.close()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--companies", type=int, default=520)
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--out", default=os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "orgchart.db"))
    args = ap.parse_args()
    os.makedirs(os.path.dirname(args.out), exist_ok=True)

    g = Gen(args.seed)
    companies = []
    for cid in range(1, args.companies + 1):
        c = g.build_company(cid)
        # shuffle so ids/order carry no hierarchy information
        g.rng.shuffle(c["people"])
        for p in c["people"]:
            p["raw_title"] = g.messy_title(p["true_title"], p["function"], p["team"],
                                           p["region"] if p["region"] and p["region"] != p["team"] else None)
            p["raw_dept"] = g.messy_dept(p["function"])
            p["raw_loc"] = g.messy_location(p["city"])
        companies.append(c)
    # renumber employee ids randomly so id order leaks nothing
    all_people = [p for c in companies for p in c["people"]]
    new_ids = list(range(1, len(all_people) + 1))
    g.rng.shuffle(new_ids)
    remap = {p["id"]: nid for p, nid in zip(all_people, new_ids)}
    for p in all_people:
        p["id"] = remap[p["id"]]
        if p["dup_of"]:
            p["dup_of"] = remap[p["dup_of"]]
    write_db(args.out, companies)
    n = len(all_people)
    print(f"wrote {args.out}: {len(companies)} companies, {n} employee rows")


if __name__ == "__main__":
    main()
