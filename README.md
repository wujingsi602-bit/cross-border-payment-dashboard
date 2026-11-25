# Cross-Border Payment Operating Dashboard (Demo)

A lightweight, 0-to-1 weekly operating & unit-economics dashboard built for an early-stage cross-border payment (FinTech) business using **Python (Pandas)**, **Excel**, and **vanilla HTML/SVG**.

> **Live Demo (GitHub Pages):** Enable GitHub Pages on `main` branch (`/root`) to view `index.html` interactively.  
> **Disclaimer:** All data in this repository is 100% synthetic (`generate_sample_data.py`). No proprietary or client data is included.

## What It Solves
- **Automated ETL (`build_dashboard.py`):** Deduplicates raw order logs, excludes internal test accounts and failed/refunded transactions, and standardizes multi-currency TPV (USD, EUR, GBP, SGD, MYR, THB) into USD.
- **Unit Economics & Margin Monitoring:** Tracks weekly TPV, take rate, and corridor gross margin to flag channel cost creep early.
- **Conversion & Churn Alerting:** Visualizes the 4-stage merchant funnel (Registration -> KYC -> First Order -> Repeat Active) and flags key accounts with >30% WoW volume decline for proactive BD outreach.

## Project Structure
- `generate_sample_data.py` — Generates 12 weeks of synthetic merchant, channel/FX rate, and transaction CSVs in `raw_data/`.
- `build_dashboard.py` — Runs the ETL pipeline and outputs `output/clean_data.xlsx` + `output/dashboard.html`.
- `index.html` — Self-contained interactive dashboard ready for GitHub Pages.
- `output/clean_data.xlsx` — Cleaned flat table and pre-aggregated summary sheets with an embedded Excel combo chart.

## Quick Start
```bash
python3 generate_sample_data.py
python3 build_dashboard.py
```
