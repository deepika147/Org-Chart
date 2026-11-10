-- Raw, vendor-style data: a flat list of people per company (no reporting lines).
CREATE TABLE companies (
  id            INTEGER PRIMARY KEY,
  name          TEXT NOT NULL,
  domain        TEXT,
  industry      TEXT,
  country       TEXT,
  hq_city       TEXT,
  founded_year  INTEGER,
  employee_count INTEGER
);

CREATE TABLE employees (
  id          INTEGER PRIMARY KEY,
  company_id  INTEGER NOT NULL REFERENCES companies(id),
  full_name   TEXT,
  title       TEXT,      -- messy, as scraped (may be NULL)
  department  TEXT,      -- messy / inconsistent / often NULL
  location    TEXT,      -- messy city spelling / NULL
  email       TEXT,
  start_date  TEXT
);
CREATE INDEX idx_emp_company ON employees(company_id);

-- Hidden answer key produced by the generator. Inference never reads it;
-- it is only used to score the inference (and shown on the Model Quality page).
CREATE TABLE ground_truth (
  employee_id     INTEGER PRIMARY KEY REFERENCES employees(id),
  true_manager_id INTEGER,
  true_function   TEXT,
  true_level      INTEGER,
  true_title      TEXT,
  true_team       TEXT,
  duplicate_of    INTEGER   -- set when this row is a duplicate scrape of another employee
);
