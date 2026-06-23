# 台灣50 (0050) 日線強度掃描器 · EV-Ranked Scanner (Live)

A self-updating GitHub Pages site that ranks the **台灣50 (0050)** constituents by
**EV (expectancy = mean per-bar close-to-close return)** across six lookback
windows (30/60/90/120/150/180 d), with live K-line charts for the EV Top-3 plus
the 0050 ETF as benchmark.

## How it works (no server, no API key)
- **`build.py`** — fetches the current 台灣50 constituents from Chinese Wikipedia
  (keyless), then pulls daily OHLC via **yfinance** and computes the EV scan +
  chart OHLC, writing `docs/scanner.json`. Falls back to a built-in 50-ticker
  list if the Wikipedia fetch fails (the page never breaks).
- **`docs/index.html`** — dark-terminal front-end; fetches same-origin
  `scanner.json` every 10 min, with a live/fallback **constituent-source badge**.
- **`.github/workflows/deploy.yml`** — GitHub Actions rebuilds `scanner.json` on a
  schedule (Taiwan market hours) + on push, and deploys to GitHub Pages.

## Data source notes
- Prices: Yahoo Finance adjusted daily (split/dividend), `.TW` suffix.
- Constituents: zh.wikipedia 臺灣50指數 (manually maintained; may lag actual
  index reconstitutions by ~one quarter). The source badge shows live vs fallback.
- Benchmark chart: 0050.TW (the ETF itself) with SMA100/200.

## Local run
```bash
pip install -r requirements.txt
python build.py            # writes docs/scanner.json
python -m http.server -d docs 8000   # open http://localhost:8000
```

⚠️ Descriptive scanner only — not a backtest, not a trading signal. Current
constituents → survivorship bias. No costs, no multiple-testing correction.
