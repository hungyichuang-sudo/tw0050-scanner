"""
台灣50 (0050) EV-Ranked 日線強度掃描器 — compute core.
Mirrors the NDX-100 scanner engine, retargeted to the Taiwan 50 universe.
Data source: yfinance (keyless, server-side; bypasses the browser CORS block on
Yahoo). Constituents are scraped fresh from Chinese Wikipedia at build time, with
a built-in fallback list. No API keys anywhere in this deployment.

Public API:
    UNIVERSE                       -> list of TW-50 tickers (constituent set, .TW)
    fetch_constituents()           -> ({ticker: name}, [ticker]) live from Wikipedia
    default_start()                -> 'YYYY-MM-DD' lookback start (~2y)
    fetch_ohlc(tickers, start)     -> {ticker: DataFrame[Open,High,Low,Close]}
    compute_scan(data)             -> {'as_of', 'windows': {N: [rows...]}}
    build_chart_ohlc(data, scan)   -> {ticker: [[date,o,h,l,c], ...]} (Top-3 union + 0050)
    build_payload(data, scan)      -> {'as_of', 'windows', 'ohlc'}
"""
import os
from datetime import datetime, timedelta, timezone
import numpy as np
import pandas as pd

# ----------------------------------------------------------------------------
# Built-in fallback universe (台灣50 constituent set, with display names).
# This is the offline FALLBACK list, used verbatim only when the live Wikipedia
# fetch in fetch_constituents() fails. Tickers carry the yfinance .TW suffix.
# Scraped from zh.wikipedia 臺灣50指數 (snapshot 2025-09-30).
# ----------------------------------------------------------------------------
FALLBACK_NAMES = {
    '2345.TW':'智邦科技','2395.TW':'研華科技','3661.TW':'世芯-KY','3711.TW':'日月光投控',
    '3017.TW':'奇鋐科技','2357.TW':'華碩電腦','6919.TW':'康霈生技','2882.TW':'國泰金控',
    '5871.TW':'中租-KY','2002.TW':'中鋼','2412.TW':'中華電信','2891.TW':'中信金控',
    '2308.TW':'台達電子','2884.TW':'玉山金控','2383.TW':'台光電','2603.TW':'長榮海運',
    '4904.TW':'遠傳電信','2892.TW':'第一金控','6505.TW':'台塑石化','1301.TW':'台塑',
    '2881.TW':'富邦金控','2317.TW':'鴻海精密','2207.TW':'和泰汽車','2880.TW':'華南金控',
    '2883.TW':'凱基金控','2059.TW':'川湖科技','3008.TW':'大立光電','2301.TW':'光寶科技',
    '2454.TW':'聯發科','2886.TW':'兆豐金控','1303.TW':'南亞塑膠','3034.TW':'聯詠科技',
    '4938.TW':'和碩聯合科技','2912.TW':'統一超商','2382.TW':'廣達電腦','2379.TW':'瑞昱半導體',
    '5876.TW':'上海商銀','2890.TW':'永豐金控','5880.TW':'合庫金控','3045.TW':'台灣大哥大',
    '2330.TW':'台積電','2887.TW':'台新金控','1216.TW':'統一企業','2303.TW':'聯電',
    '2615.TW':'萬海航運','3231.TW':'緯創資通','6669.TW':'緯穎科技','2327.TW':'國巨',
    '2609.TW':'陽明海運','2885.TW':'元大金控'
}

# Active constituent maps. At import these mirror the built-in fallback so the
# module stays usable standalone; main() refreshes them from Wikipedia at build
# time (with fallback to FALLBACK_NAMES on any error).
NAMES = dict(FALLBACK_NAMES)
UNIVERSE = list(NAMES.keys())

WINDOWS = [30, 60, 90, 120, 150, 180]
TRADING = 252
CHART_BARS = 520          # bars kept per ticker for the front-end candles (~2y; covers SMA200)
BENCH = '0050.TW'         # benchmark chart = the 0050 ETF itself


def default_start():
    """~2 years of history: enough for the 180d window + SMA200 + warmup."""
    return (datetime.utcnow() - timedelta(days=760)).strftime('%Y-%m-%d')


# ----------------------------------------------------------------------------
# Live constituents (Wikipedia zh, keyless). Scraped fresh at build time so the
# scanner follows index reconstitutions automatically. Returns
# ({ticker: name}, [ticker, ...]); raises on any network/parse/empty error so
# main() can fall back to the built-in FALLBACK_NAMES list.
#
# The zh.wikipedia 臺灣50指數 page carries the constituents in a paired-column
# wikitable (股票代號 | 名稱 | 比重 | 股票代號 | 名稱 | 比重) — two stocks per
# row. We identify that table by its column names and un-pivot the two halves
# into a single {ticker: name} map. Ticker cells read like "臺證所：2330", so we
# pull the 4-6 digit code out and append the yfinance .TW suffix.
# ----------------------------------------------------------------------------
WIKI_TW50_URL = 'https://zh.wikipedia.org/wiki/臺灣50指數'


def _extract_code(cell):
    import re
    m = re.search(r'(\d{4,6})', str(cell))
    return m.group(1) if m else None


def fetch_constituents(url=None):
    """Scrape the current 台灣50 components from Chinese Wikipedia."""
    import requests
    from io import StringIO
    url = url or os.environ.get('TW50_CONSTITUENTS_URL') or WIKI_TW50_URL
    resp = requests.get(url, headers={'User-Agent':
        'Mozilla/5.0 (compatible; tw0050-scanner/1.0)'}, timeout=30)
    resp.raise_for_status()
    tables = pd.read_html(StringIO(resp.text))

    def norm(c):
        return str(c).strip()

    # Find the constituents table: needs at least one 股票代號 + one 名稱 column.
    chosen = None
    for t in tables:
        cols = [norm(c) for c in t.columns]
        has_code = any('股票代號' in c or '代號' in c for c in cols)
        has_name = any(c == '名稱' or c.startswith('名稱') for c in cols)
        if has_code and has_name:
            chosen = t
            break
    if chosen is None:
        raise ValueError('no 台灣50 constituents table (股票代號/名稱) found')

    cols = [norm(c) for c in chosen.columns]
    # Collect (code_col_idx, name_col_idx) pairs across the paired layout.
    code_idx = [i for i, c in enumerate(cols) if ('股票代號' in c or '代號' in c)]
    name_idx = [i for i, c in enumerate(cols) if (c == '名稱' or c.startswith('名稱'))]
    pairs = list(zip(sorted(code_idx), sorted(name_idx)))
    if not pairs:
        raise ValueError('台灣50 table found but no code/name column pairs')

    names = {}
    for _, row in chosen.iterrows():
        for ci, ni in pairs:
            code = _extract_code(row.iloc[ci])
            nm = str(row.iloc[ni]).replace('\xa0', ' ').strip()
            if not code or nm.lower() == 'nan':
                continue
            tk = code + '.TW'
            names.setdefault(tk, nm or tk)
    if not names:
        raise ValueError('台灣50 components table parsed but yielded 0 tickers')
    return names, list(names.keys())


# ----------------------------------------------------------------------------
# Fetch (yfinance). Returns {ticker: DataFrame[Open,High,Low,Close]} ascending.
# ----------------------------------------------------------------------------
def fetch_ohlc(tickers, start=None):
    import yfinance as yf
    start = start or default_start()
    tickers = list(dict.fromkeys(tickers))      # dedupe, keep order
    df = yf.download(tickers, start=start, interval='1d', auto_adjust=True,
                     progress=False, threads=True, group_by='ticker')
    out = {}
    for tk in tickers:
        try:
            sub = df[tk] if isinstance(df.columns, pd.MultiIndex) else df
        except Exception:
            continue
        sub = sub.dropna(subset=['Open', 'High', 'Low', 'Close'])
        if len(sub) == 0:
            continue
        out[tk] = sub[['Open', 'High', 'Low', 'Close']].copy()
    return out


# ----------------------------------------------------------------------------
# Metric helpers (verbatim from the NDX scanner)
# ----------------------------------------------------------------------------
def runs_cumret(r):
    ups, downs = [], []
    i, n = 0, len(r)
    while i < n:
        if r[i] > 0:
            j = i
            while j < n and r[j] > 0: j += 1
            ups.append(np.prod(1.0 + r[i:j]) - 1.0); i = j
        elif r[i] < 0:
            j = i
            while j < n and r[j] < 0: j += 1
            downs.append(np.prod(1.0 + r[i:j]) - 1.0); i = j
        else:
            i += 1
    return ups, downs


def max_drawdown(closes):
    peak = closes[0]; mdd = 0.0
    for c in closes:
        if c > peak: peak = c
        dd = c / peak - 1.0
        if dd < mdd: mdd = dd
    return mdd


def ols_slope(y):
    n = len(y)
    if n < 3: return 0.0
    x = np.arange(n); xm, ym = x.mean(), y.mean()
    denom = ((x - xm) ** 2).sum()
    if denom == 0: return 0.0
    return float(((x - xm) * (y - ym)).sum() / denom)


# ----------------------------------------------------------------------------
# Scan (verbatim metric logic from the NDX scanner)
# ----------------------------------------------------------------------------
def compute_scan(data):
    asof = max(v.index[-1] for v in data.values())
    asof_str = pd.Timestamp(asof).strftime('%Y-%m-%d')
    out = {'as_of': asof_str, 'windows': {}}

    for N in WINDOWS:
        rows = []
        roll_w = max(10, round(N / 4))
        for t, df in data.items():
            if t == BENCH:
                continue  # benchmark is charted, not ranked
            C = df['Close'].values.astype(float)
            O = df['Open'].values.astype(float)
            if len(C) < N + 2:
                continue
            ret_full = C[1:] / C[:-1] - 1.0
            r = ret_full[-N:]
            Cwin = C[-(N + 1):]
            Owin = O[-N:]
            Cprev = C[-(N + 1):-1]
            gaps = (Owin - Cprev) / Cprev
            green_body = np.mean(C[-N:] > Owin)
            p = float(np.mean(r > 0))
            up = r[r > 0]; dn = r[r < 0]
            avg_up = float(up.mean()) if up.size else 0.0
            avg_dn = float(-dn.mean()) if dn.size else 0.0
            ev = float(np.mean(r))
            period_ret = float(C[-1] / C[-(N + 1)] - 1.0)
            mdd = float(max_drawdown(Cwin))
            ups, downs = runs_cumret(r)
            up_swing = float(np.mean(ups)) if ups else 0.0
            pullback = float(np.mean(np.abs(downs))) if downs else 0.0
            avg_gap = float(np.mean(gaps))
            sd = float(np.std(r, ddof=1)) if r.size > 1 else 0.0
            sharpe = float(ev / sd * np.sqrt(TRADING)) if sd > 0 else 0.0
            neg = np.minimum(r, 0.0)
            dd_dev = float(np.sqrt(np.mean(neg ** 2)))
            sortino = float(ev / dd_dev * np.sqrt(TRADING)) if dd_dev > 0 else (sharpe if sd > 0 else 0.0)
            roll = [float(np.mean(r[i - roll_w + 1:i + 1])) for i in range(roll_w - 1, N)]
            roll = np.array(roll)
            ev_slope_bps = ols_slope(roll) * 10000.0
            spark = roll * 10000.0
            if len(spark) > 40:
                idx = np.linspace(0, len(spark) - 1, 40).round().astype(int)
                spark = spark[idx]
            spark = [round(float(x), 2) for x in spark]
            rows.append({
                't': t, 'name': NAMES.get(t, t),
                'last': round(float(C[-1]), 2),
                'ret': round(period_ret * 100, 2),
                'win': round(p * 100, 1),
                'green': round(float(green_body) * 100, 1),
                'aup': round(avg_up * 100, 3),
                'adn': round(avg_dn * 100, 3),
                'uswing': round(up_swing * 100, 2),
                'pull': round(pullback * 100, 2),
                'gap': round(avg_gap * 100, 3),
                'mdd': round(mdd * 100, 2),
                'ev': round(ev * 100, 4),
                'evbps': round(ev * 10000, 1),
                'evslope': round(ev_slope_bps, 3),
                'sharpe': round(sharpe, 2),
                'sortino': round(sortino, 2),
                'spark': spark,
            })
        rows.sort(key=lambda x: x['ev'], reverse=True)
        for i, row in enumerate(rows):
            row['rank'] = i + 1
        out['windows'][str(N)] = rows
    return out


# ----------------------------------------------------------------------------
# Chart OHLC subset: union of each window's EV Top-3, plus the 0050 benchmark.
# ----------------------------------------------------------------------------
def build_chart_ohlc(data, scan, n_top=3):
    need = set([BENCH])
    for N in WINDOWS:
        rows = scan['windows'].get(str(N), [])
        for row in rows[:n_top]:
            need.add(row['t'])
    ohlc = {}
    for tk in need:
        df = data.get(tk)
        if df is None or len(df) == 0:
            continue
        sub = df.tail(CHART_BARS)
        bars = []
        for idx, row in sub.iterrows():
            bars.append([pd.Timestamp(idx).strftime('%Y-%m-%d'),
                         round(float(row['Open']), 2), round(float(row['High']), 2),
                         round(float(row['Low']), 2), round(float(row['Close']), 2)])
        ohlc[tk] = bars
    return ohlc


def build_payload(data, scan):
    return {'as_of': scan['as_of'], 'windows': scan['windows'],
            'ohlc': build_chart_ohlc(data, scan)}


# ============================================================================
# Build entrypoint — writes docs/scanner.json
# Run by the GitHub Actions workflow. Also usable locally: `python build.py`.
# ============================================================================
import json

def main():
    global NAMES, UNIVERSE
    # Refresh the constituent universe from Wikipedia at build time so the
    # scanner tracks index reconstitutions. Any failure falls back to the
    # built-in list, keeping the build (and the live site) healthy.
    try:
        names, tickers = fetch_constituents()
        NAMES = names
        UNIVERSE = tickers
        source_mode = 'live'
        print('fetched %d 台灣50 constituents from Wikipedia' % len(tickers))
    except Exception as e:
        print('WARN: constituent fetch failed, using built-in fallback list (%s)' % e)
        NAMES = dict(FALLBACK_NAMES)
        UNIVERSE = list(FALLBACK_NAMES.keys())
        source_mode = 'fallback'
    fetched_at = datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')

    uni = UNIVERSE
    lim = os.environ.get('UNIVERSE_LIMIT')
    if lim:
        uni = uni[:int(lim)]
    start = os.environ.get('HIST_START') or default_start()

    data = fetch_ohlc(uni + [BENCH], start=start)
    if not data:
        raise SystemExit('fetch_ohlc returned no data; aborting (keeps last good deploy)')

    scan = compute_scan(data)
    payload = build_payload(data, scan)
    # Surface the constituent-source status (live vs built-in fallback) so the
    # page can flag silent fallbacks. Additive only — as_of/windows/ohlc unchanged.
    payload['source'] = {'mode': source_mode, 'count': len(UNIVERSE), 'fetched_at': fetched_at}

    here = os.path.dirname(os.path.abspath(__file__))
    out = os.path.join(here, 'docs', 'scanner.json')
    os.makedirs(os.path.dirname(out), exist_ok=True)
    with open(out, 'w') as f:
        json.dump(payload, f, separators=(',', ':'), ensure_ascii=False)

    kb = round(os.path.getsize(out) / 1024, 1)
    print('wrote', out, '(%s KB)' % kb)
    print('as_of', payload['as_of'], '| universe', len(payload['windows']['90']),
          '| source', payload['source'], '| chart tickers', sorted(payload['ohlc'].keys()))

if __name__ == '__main__':
    main()
