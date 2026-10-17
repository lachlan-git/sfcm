"""Build a US money-flow network from BEA/Fed data and write web/data/flows_<year>.js.

All dollar values are in $ millions (BEA convention) until written out.
Every modelling choice that is not straight from the data is recorded in ASSUMPTIONS
and shipped to the browser so it can be audited.
"""

import argparse
import csv
import json
import re
from collections import defaultdict
from pathlib import Path

import numpy as np
import openpyxl
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
RAW = ROOT / "data" / "raw"
OUT_DIR = ROOT / "web" / "data"
YEAR = 2025  # overridden by --year
IO_DETAIL_YEAR = 2017  # only year BEA publishes the 402-industry detail tables
SUMMARY_LAST = (
    2023  # last year of BEA's summary Use/Make tables; later years are projected
)
SOURCE_YEARS = {}

# ---- Tunable assumptions (all $ millions unless noted) -------------------------------
FRB_DWELL_DAYS = 7.0  # Fed remits earnings to Treasury weekly
# Federal Reserve Banks' income statement ($M). 2023-2025: audited Combined Statements of Operations
# (tsy = Treasury securities; mbs = agency MBS + GSE debt; reserves = "depository institutions and
# others"; repo = securities sold under repurchase agreements). 2017: income & expense press release,
# securities income split Treasury/MBS by Z.1 holdings.
FED_FINANCIALS = {
    2017: {"securities_income": 113.6e3, "int_reserves": 25.9e3, "int_repo": 4.0e3},
    2023: {"tsy": 106_479, "mbs": 57_148, "int_reserves": 176_755, "int_repo": 104_341},
    2024: {"tsy": 100_518, "mbs": 52_778, "int_reserves": 186_478, "int_repo": 40_288},
    2025: {"tsy": 106_560, "mbs": 48_158, "int_reserves": 147_662, "int_repo": 19_719},
}

# BEA summary IO industry -> line number in the GDP-by-industry tables (TGO105 / TVA105)
GDPIND_LINES = {
    "111CA": 4, "113FF": 5, "211": 7, "212": 8, "213": 9, "22": 10, "23": 11,
    "321": 14, "327": 15, "331": 16, "332": 17, "333": 18, "334": 19, "335": 20,
    "3361MV": 21, "3364OT": 22, "337": 23, "339": 24, "311FT": 26, "313TT": 27,
    "315AL": 28, "322": 29, "323": 30, "324": 31, "325": 32, "326": 33, "42": 34,
    "441": 36, "445": 37, "452": 38, "4A0": 39, "481": 41, "482": 42, "483": 43,
    "484": 44, "485": 45, "486": 46, "487OS": 47, "493": 48, "511": 50, "512": 51,
    "513": 52, "514": 53, "521CI": 56, "523": 57, "524": 58, "525": 59, "HS": 62,
    "ORE": 63, "532RL": 64, "5411": 67, "5415": 68, "5412OP": 69, "55": 70, "561": 72,
    "562": 73, "61": 75, "621": 77, "622": 78, "623": 79, "624": 80, "711AS": 83,
    "713": 84, "721": 86, "722": 87, "81": 88, "GFGD": 92, "GFGN": 93, "GFE": 94,
    "GSLG": 96, "GSLE": 97,
}  # fmt: skip
# summary final-demand column (or 3-char prefix) -> NIPA aggregate used to project it
FD_NIPA = {
    "F010": "DPCERC",
    "F02E": "Y033RC",
    "F02N": "Y001RC",
    "F02R": "A011RC",
    "F02S": "B009RC",
    "F030": "A014RC",
    "F040": "B020RC",
    "F050": "B021RC",
    "F06": "A824RC",
    "F07": "A825RC",
    "F10": "A829RC",
}

NODE_URLS = {
    "FED_SRC": "https://fiscaldata.treasury.gov/americas-finance-guide/federal-spending/",
    "FED_SINK": "https://fiscaldata.treasury.gov/americas-finance-guide/government-revenue/",
    "SL": "https://www.census.gov/programs-surveys/gov-finances.html",
    "FRB": "https://www.federalreserve.gov/aboutthefed/audited-annual-financial-statements.htm",
    "ROW_SRC": "https://www.bea.gov/data/intl-trade-investment/international-transactions",
    "ROW_SINK": "https://www.bea.gov/data/intl-trade-investment/international-transactions",
    "CREDIT": "https://www.bankofengland.co.uk/quarterly-bulletin/2014/q1/money-creation-in-the-modern-economy",
    "SAVING": "https://www.bankofengland.co.uk/quarterly-bulletin/2014/q1/money-creation-in-the-modern-economy",
    "PEN_SRC": "https://www.dol.gov/general/topic/retirement/typesofplans",
    "PEN_SINK": "https://www.dol.gov/general/topic/retirement/typesofplans",
    "HH": "https://www.bea.gov/data/special-topics/distribution-of-personal-income",
    "GOV_IND": "https://www.bea.gov/data/gdp/gdp-industry",
}
HH_INTEREST_DEPOSITORY = (
    0.7  # share of household interest payments going to banks (rest non-bank lenders)
)
GOV_HEALTH_CAP = (
    0.85  # Medicare+Medicaid can fund at most this share of PCE on health providers
)
MIN_EDGE = 25.0  # drop edges below $25M/yr (except each node's top 3 outflows)

HH_GROUPS = [
    "0-10%",
    "10-20%",
    "20-30%",
    "30-40%",
    "40-50%",
    "50-60%",
    "60-70%",
    "70-80%",
    "80-90%",
    "90-95%",
    "95-99%",
    "Top 1%",
]
HH_QUINTILE = [0, 0, 1, 1, 2, 2, 3, 3, 4, 4, 4, 4]
# Fed Distributional Financial Accounts income bands -> indices into HH_GROUPS
DFA_BANDS = {
    "pct00to20": [0, 1],
    "pct20to40": [2, 3],
    "pct40to60": [4, 5],
    "pct60to80": [6, 7],
    "pct80to99": [8, 9, 10],
    "pct99to100": [11],
}
Z1_SERIES = {
    "hh_chk": "FL153020005",
    "hh_ts": "FL153030005",
    "nfc_chk": "FL103020005",
    "nfc_ts": "FL103030003",
    "nnc_chk": "FL113020005",
    "nnc_ts": "FL113030003",
    "sl_chk": "FL213020005",
    "sl_ts": "FL213030000",
    "fed_tsy": "FL713061163",
    "fed_agency": "FL713061765",
}

# Spending tilt by income quintile relative to the average household (approximate, informed by
# BLS Consumer Expenditure Survey 2017 quintile budget shares; entered by hand because BLS
# blocks automated downloads). RAS-balanced afterwards so BEA totals are preserved exactly.
TILTS = {
    "food_home": [1.30, 1.15, 1.05, 0.95, 0.80],
    "food_away": [0.90, 0.95, 1.00, 1.02, 1.03],
    "rent": [1.60, 1.30, 1.05, 0.85, 0.60],
    "utilities": [1.45, 1.30, 1.10, 0.95, 0.75],
    "gasoline": [1.25, 1.25, 1.15, 1.00, 0.75],
    "vehicles": [0.75, 0.95, 1.05, 1.10, 1.00],
    "health": [1.15, 1.25, 1.10, 1.00, 0.85],
    "education": [1.60, 0.70, 0.70, 0.85, 1.15],
    "recreation": [0.85, 0.90, 0.95, 1.00, 1.10],
    "finance": [0.60, 0.75, 0.90, 1.00, 1.25],
    "apparel": [1.05, 0.95, 0.95, 1.00, 1.03],
    "travel": [0.60, 0.70, 0.85, 1.00, 1.35],
    "other": [1.00, 1.00, 1.00, 1.00, 1.00],
}
CATEGORY_PREFIXES = [  # first match wins
    ("food_away", ["722"]),
    ("food_home", ["111", "112", "311", "312", "445"]),
    ("rent", ["531HST", "531ORE"]),
    ("utilities", ["221", "517"]),
    ("gasoline", ["324", "447"]),
    ("vehicles", ["3361", "3362", "3363", "441", "8111"]),
    ("health", ["621", "622", "623", "3254", "446", "5241"]),
    ("education", ["61"]),
    ("recreation", ["71", "512", "515", "3399"]),
    ("finance", ["52"]),
    ("apparel", ["315", "316", "448"]),
    ("travel", ["721", "481", "5615"]),
]

NIPA_SERIES = {
    "fed_personal_taxes": "A074RC",
    "fed_tpi": "W007RC",
    "fed_corp": "B075RC",
    "fed_frb_remit": "LA000248",
    "fed_benefits": "B087RC",
    "grants": "B089RC",
    "fed_interest": "A091RC",
    "fed_int_row": "B093RC",
    "fed_transfers_row": "W016RC",
    "fed_other_transfers_row": "W017RC",
    "medicare": "W824RC",
    "medicaid": "W729RC",
    "sl_personal_taxes": "W071RC",
    "sl_tpi": "W072RC",
    "sl_corp": "B102RC",
    "sl_benefits": "B109RC",
    "sl_interest": "Y705RC",
    "comp": "A033RC",
    "wages": "A034RC",
    "emp_si": "B039RC",
    "emp_pension": "B040RC",
    "contrib_total": "A061RC",
    "pers_interest": "A064RC",
    "pers_div": "B703RC",
    "proprietors": "A041RC",
    "hh_interest_paid": "B069RC",
    "hh_transfers_gov": "W062RC",
    "hh_transfers_row": "B070RC",
    "pce": "DPCERC",
    "exports": "B020RC",
    "imports": "B021RC",
    # NIPA 7.20 (all pension plans) and 7.23 (federal plans)
    "pen_emp": "Y203RC",
    "pen_imp_emp": "Y604RC",
    "pen_hh": "Y208RC",
    "pen_mint": "Y210RC",
    "pen_imp_int": "Y609RC",
    "pen_div": "Y234RC",
    "pen_benefits": "Y235RC",
    "pen_admin": "Y204RC",
    "fpen_emp": "Y270RC",
    "fpen_hh": "Y276RC",
    "fpen_mint": "Y281RC",
    "fpen_benefits": "Y293RC",
}

ASSUMPTIONS = []
NOTES = {}

# BEA Fixed Assets industry code(s) -> BEA IO detail-code prefixes (longest prefix wins)
FA_PREFIXES = {
    "110C": ["111", "112"],
    "113F": ["113", "114", "115"],
    "2110": ["211"],
    "2120": ["212"],
    "2130": ["213"],
    "2211": ["2211"],
    "2212": ["2212"],
    "2213": ["2213"],
    "2300": ["23"],
    "3210": ["321"],
    "3270": ["327"],
    "3310": ["331"],
    "3320": ["332"],
    "3330": ["333"],
    "3340": ["334"],
    "3350": ["335"],
    "336M": ["3361", "3362", "3363"],
    "336O": ["3364", "3365", "3366", "3369"],
    "3370": ["337"],
    "3380": ["3391"],
    "3390": ["3399"],
    "3110": ["311"],
    "3120": ["312"],
    "313T": ["313", "314"],
    "315A": ["315", "316"],
    "3220": ["322"],
    "3230": ["323"],
    "3240": ["324"],
    "3250": ["325"],
    "3260": ["326"],
    "4210": ["423", "425"],
    "4220": ["424"],
    "4410": ["441"],
    "4450": ["445"],
    "4520": ["452"],
    "442R": ["44", "45", "4A"],
    "4810": ["481"],
    "4820": ["482"],
    "4830": ["483"],
    "4840": ["484"],
    "4850": ["485"],
    "4860": ["486"],
    "487S": ["487", "488", "492"],
    "4930": ["493"],
    "5110": ["511"],
    "5120": ["512"],
    "5130": ["515", "517"],
    "5140": ["518", "519"],
    "5210+5221+5223": ["52A"],
    "5229": ["522"],
    "5230": ["523"],
    "524A": ["5241"],
    "5243": ["5242"],
    "5250": ["525"],
    "5310": ["531"],
    "5320": ["532", "533"],
    "5411": ["5411"],
    "5415": ["5415"],
    "5412": ["541"],
    "5500": ["55"],
    "5610": ["561"],
    "5620": ["562"],
    "6100": ["61"],
    "6210": ["621"],
    "622H": ["622"],
    "6230": ["623"],
    "6240": ["624"],
    "711A": ["711", "712"],
    "7130": ["713"],
    "7210": ["721"],
    "7220": ["722"],
    "8100": ["81"],
}
FA_CATEGORIES = {"EQUIPMENT": "F02E00", "STRUCTURES": "F02S00", "IPP": "F02N00"}
# BEA fixed-asset type -> IO commodity-code prefixes it is bought as. Commodities not claimed by
# any asset (trade/transport margins, installation, brokers' fees) are shared across all assets.
ASSET_COMMODITIES = {
    # equipment
    "EP1A": ["3341"],
    "EP1B": ["3341"],
    "EP1C": ["3341"],
    "EP1D": ["3341"],
    "EP1E": ["3341"],
    "EP1F": ["3341"],
    "EP1G": ["3341"],
    "EP1H": ["541512"],
    "EP20": ["3342"],
    "EP34": ["3391"],
    "EP35": ["334510", "334517"],
    "EP36": ["334511", "334513", "334514", "334515", "334516", "33451A"],
    "EP31": ["333316", "333314"],
    "EP12": ["339940", "333318"],
    "EI11": ["325180"],
    "EI12": ["332"],
    "EI21": ["333611", "332410"],
    "EI22": ["333618"],
    "EI30": ["3335"],
    "EI40": ["3332", "333993"],
    "EI50": ["3339", "3334"],
    "EI60": ["3353", "3359"],
    "ET11": ["336112"],
    "ET12": ["336120", "3362"],
    "ET20": ["336111"],
    "ET30": ["3364"],
    "ET40": ["3366"],
    "ET50": ["336500"],
    "EO11": ["33712", "337110"],
    "EO12": ["3372", "337127", "337900"],
    "EO30": ["333111", "333112"],
    "EO21": ["333111"],
    "EO40": ["333120"],
    "EO22": ["333120"],
    "EO50": ["333130"],
    "EO60": ["333318", "333415", "3334"],
    "EO71": ["3352"],
    "EO72": ["3351", "3359"],
    "EO80": ["339", "3369", "3343", "314110", "326"],
    # structures
    "SOO1": ["2332A0"],
    "SB31": ["233210"],
    "SB32": ["233210"],
    "SOO2": ["233210"],
    "SC03": ["2332A0"],
    "SC04": ["2332A0"],
    "SC01": ["2332A0"],
    "SOMO": ["2332A0"],
    "SC02": ["2332A0"],
    "SI00": ["233230"],
    "SU30": ["233240"],
    "SU60": ["233240"],
    "SU40": ["233240"],
    "SU50": ["233240"],
    "SU20": ["233240"],
    "SM01": ["213111", "21311A"],
    "SM02": ["21311A"],
    "SB10": ["2332D0"],
    "SB20": ["233262"],
    "SB41": ["2332A0"],
    "SB42": ["2332D0"],
    "SB43": ["2332C0"],
    "SB45": ["2332C0"],
    "SU11": ["2332C0"],
    "SU12": ["2332C0"],
    "SB44": ["2332C0"],
    "SB46": ["2332C0"],
    "SN00": ["2332D0"],
    "SO01": ["2332D0"],
    "SO02": ["2332D0"],
    "SO03": ["2332D0"],
    "SO04": ["2332C0"],
    # intellectual property products (RD* handled by prefix below)
    "ENS1": ["511200"],
    "ENS2": ["541511"],
    "ENS3": ["541511"],
    "AE10": ["512100"],
    "AE20": ["512100", "515"],
    "AE30": ["5111"],
    "AE40": ["512200", "711500"],
    "AE50": ["711", "5419"],
}


def asset_commodities(asset):
    if asset.startswith("RD"):
        return ["541700"]
    return ASSET_COMMODITIES.get(asset, [])


def assume(title, points):
    ASSUMPTIONS.append({"title": title, "points": [p for p in points if p]})


def code_str(v):
    if isinstance(v, float) and v.is_integer():
        v = int(v)
    return str(v).strip()


# ---- Loaders ---------------------------------------------------------------------------
def nipa_values(codes, year):
    vals = {}
    wanted = set(codes)
    with open(RAW / "NipaDataA.txt", encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            if row["%SeriesCode"] in wanted and row["Period"] == str(year):
                vals[row["%SeriesCode"]] = float(row["Value"].replace(",", ""))
    missing = wanted - set(vals)
    if missing:
        raise SystemExit(f"Missing NIPA series for {year}: {missing}")
    return vals


def load_nipa():
    vals = nipa_values(NIPA_SERIES.values(), YEAR)
    return {k: vals[v] for k, v in NIPA_SERIES.items()}


def load_hierarchy():
    x = pd.read_excel(
        RAW / "io" / "IOUse_Before_Redefinitions_PRO_2017_Detail.xlsx",
        sheet_name="NAICS Codes",
        header=None,
    )
    sector = summary = summary_name = None
    info = {}
    for _, r in x.iloc[5:].iterrows():
        c0, c1, c2, c3, c4 = r[0], r[1], r[2], r[3], r[4]
        if pd.notna(c0) and pd.notna(c1):
            sector = code_str(c0)
        if pd.notna(c1) and pd.notna(c2):
            summary, summary_name = code_str(c1), str(c2).strip()
        if pd.notna(c3) and pd.notna(c4):
            naics = re.match(r"\d+", str(r[6]).strip()) if pd.notna(r[6]) else None
            info[code_str(c3)] = {
                "sector": sector,
                "summary": summary,
                "summary_name": summary_name,
                "name": str(c4).strip(),
                "naics": naics.group(0) if naics else None,
            }
    return info


def load_use():
    u = pd.read_excel(
        RAW / "io" / "IOUse_Before_Redefinitions_PRO_2017_Detail.xlsx",
        sheet_name=str(IO_DETAIL_YEAR),
        header=None,
    )
    col_codes = [code_str(c) if pd.notna(c) else None for c in u.iloc[5]]
    row_codes = [code_str(c) if pd.notna(c) else None for c in u.iloc[:, 0]]
    t001 = col_codes.index("T001")
    ind_cols = list(range(2, t001))
    fd_cols = [
        j
        for j in range(t001 + 1, len(col_codes))
        if col_codes[j] and col_codes[j].startswith("F")
    ]
    t005 = row_codes.index("T005")
    com_rows = list(range(6, t005))
    vals = u.apply(pd.to_numeric, errors="coerce").fillna(0.0).to_numpy()
    return {
        "industries": [col_codes[j] for j in ind_cols],
        "commodities": [row_codes[i] for i in com_rows],
        "fd": [col_codes[j] for j in fd_cols],
        "Z": vals[np.ix_(com_rows, ind_cols)],
        "F": vals[np.ix_(com_rows, fd_cols)],
        "VA": {
            c: vals[row_codes.index(c), ind_cols]
            for c in ["V00100", "V00200", "V00300", "T008"]
        },
    }


def load_make(industries, commodities):
    m = pd.read_excel(
        RAW / "io" / "IOMake_Before_Redefinitions_2017_Detail.xlsx",
        sheet_name=str(IO_DETAIL_YEAR),
        header=None,
    )
    col_codes = [code_str(c) if pd.notna(c) else None for c in m.iloc[5]]
    row_codes = [code_str(c) if pd.notna(c) else None for c in m.iloc[:, 0]]
    vals = m.apply(pd.to_numeric, errors="coerce").fillna(0.0).to_numpy()
    M = np.zeros((len(industries), len(commodities)))
    for a, ind in enumerate(industries):
        if ind not in row_codes:
            continue
        ri = row_codes.index(ind)
        for b, com in enumerate(commodities):
            if com in col_codes:
                M[a, b] = vals[ri, col_codes.index(com)]
    return M


def load_summary(kind, year):
    """BEA summary Use or Make table for a year as {(row code, col code): $M}."""
    df = pd.read_excel(
        RAW / "io" / f"IO{kind}_Before_Redefinitions_PRO_1997-2023_Summary.xlsx",
        sheet_name=str(year),
        header=None,
    )
    cols = [code_str(c) if pd.notna(c) else None for c in df.iloc[5]]
    rows = [code_str(c) if pd.notna(c) else None for c in df.iloc[:, 0]]
    vals = df.apply(pd.to_numeric, errors="coerce").fillna(0.0).to_numpy()
    return {
        (r, c): vals[i, j]
        for i, r in enumerate(rows)
        if i >= 7 and r
        for j, c in enumerate(cols)
        if j >= 2 and c
    }


def gdpind_table(file, sheet):
    """BEA GDP-by-industry table as a function (line number, year) -> $M or percent."""
    df = pd.read_excel(RAW / "gdpind" / file, sheet_name=sheet, header=None)
    hdr = next(i for i in range(len(df)) if str(df.iat[i, 0]).strip() == "Line")
    ycol = {}
    for j, v in enumerate(df.iloc[hdr]):
        try:
            ycol[int(float(v))] = j
        except (TypeError, ValueError):
            pass
    rows = {}
    for i in range(hdr + 1, len(df)):
        try:
            rows[int(float(df.iat[i, 0]))] = i
        except (TypeError, ValueError):
            pass
    vals = df.apply(pd.to_numeric, errors="coerce")  # '.....' = suppressed -> NaN
    return lambda line, year: float(vals.iat[rows[line], ycol[year]])


def project_summary(su, sm):
    """Project the SUMMARY_LAST summary Use/Make tables to YEAR (a nowcast)."""
    y0 = SUMMARY_LAST
    go = gdpind_table("GrossOutput.xlsx", "TGO105-A")
    va = gdpind_table("ValueAdded.xlsx", "TVA105-A")
    va_pct = gdpind_table("ValueAdded.xlsx", "TVA112-A")
    n0, n1 = nipa_values(FD_NIPA.values(), y0), nipa_values(FD_NIPA.values(), YEAR)
    va_rows = {"V001": 1, "V002": 2, "V003": 3}
    use = {}
    for (r, c), v in su.items():
        if c in GDPIND_LINES:
            L = GDPIND_LINES[c]
            if (
                r in va_rows
            ):  # TVA112 lays out each industry as 4 lines: total + 3 components
                pct = va_pct(4 * (L - 1) + 1 + va_rows[r], YEAR)
                use[(r, c)] = (
                    va(L, YEAR) * pct / 100
                    if np.isfinite(pct)
                    else v * va(L, YEAR) / va(L, y0)
                )
            else:
                ii0, ii1 = go(L, y0) - va(L, y0), go(L, YEAR) - va(L, YEAR)
                use[(r, c)] = v * ii1 / ii0
        else:
            key = c if c in FD_NIPA else c[:3]
            s = FD_NIPA.get(key)
            use[(r, c)] = v * n1[s] / n0[s] if s and n0[s] else v
    make = {}
    for (r, c), v in sm.items():
        L = GDPIND_LINES.get(r)
        make[(r, c)] = v * go(L, YEAR) / go(L, y0) if L else v
    unmatched = {c for (_, c) in su if c and c[0].isdigit()} - set(GDPIND_LINES)
    if unmatched:
        print("warning: summary industries without GDP-by-industry line:", unmatched)
    return use, make


def scale_blocks(M, row_codes, col_codes, row_map, col_map, target):
    """Rescale each summary-level block of a detail matrix so it sums to the target year's value."""
    R = [row_map(c) for c in row_codes]
    C = [col_map(c) for c in col_codes]
    block = defaultdict(float)
    nz = list(zip(*np.nonzero(M)))
    for i, j in nz:
        block[(R[i], C[j])] += M[i, j]
    out = np.zeros_like(M)
    for i, j in nz:
        s = block[(R[i], C[j])]
        if abs(s) > 1e-9:
            out[i, j] = M[i, j] * target.get((R[i], C[j]), 0.0) / s
    rset, cset = set(R), set(C)
    relevant = {k: v for k, v in target.items() if k[0] in rset and k[1] in cset}
    lost = sum(abs(v) for k, v in relevant.items() if abs(block.get(k, 0.0)) <= 1e-9)
    return out, lost, sum(abs(v) for v in relevant.values())


def load_z1():
    """Fed Financial Accounts (Z.1) levels, averaged over the four quarters of YEAR."""
    out = {}
    for fname in ("F2_2_s.csv", "F2_3_s.csv", "S121_s.csv"):
        df = pd.read_csv(RAW / "z1" / "csv" / fname)
        df = df[df["date"].astype(str).str.startswith(str(YEAR))]
        for k, code in Z1_SERIES.items():
            if k not in out and f"{code}.Q" in df.columns:
                out[k] = float(pd.to_numeric(df[f"{code}.Q"], errors="coerce").mean())
    missing = set(Z1_SERIES) - set(out)
    if missing:
        raise SystemExit(f"Missing Z.1 series: {missing}")
    return out


def load_dfa_deposits(dpi_shares):
    """Household deposits by HH_GROUPS from the Fed's Distributional Financial Accounts (income)."""
    d = pd.read_csv(RAW / "dfa" / "dfa-income-levels-detail.csv")
    d = d[d["Date"].astype(str).str.startswith(str(YEAR))]
    band = d.groupby("Category")["Deposits"].mean()
    dep = np.zeros(len(HH_GROUPS))
    for cat, idx in DFA_BANDS.items():
        w = dpi_shares[idx] / dpi_shares[idx].sum()
        dep[idx] = band[cat] * w
    return dep


def load_household_shares():
    d = pd.read_csv(RAW / "dpi_summary.csv")
    dpi_year = max(y for y in d.Year.unique() if y <= YEAR)
    SOURCE_YEARS["distribution of personal income"] = int(dpi_year)
    d = d[
        (d.Year == dpi_year) & (d.Ranking == "Equivalized Disposable Personal Income")
    ]

    def num(
        v,
    ):  # recent years publish top shares as ranges, e.g. "[0.137-0.143]": use the midpoint
        m = re.fullmatch(r"\[([\d.]+)-([\d.]+)\]", str(v).strip())
        return (
            (float(m[1]) + float(m[2])) / 2 if m else pd.to_numeric(v, errors="coerce")
        )

    NOTES["dpi_ranges"] = bool(d["Value"].astype(str).str.startswith("[").any())
    d["Value"] = d["Value"].map(num).astype(float)

    dpi = dict(
        zip(
            *d[d["Income Concept"] == "Disposable personal income"][
                ["Quantile or Summary Metric", "Value"]
            ].T.values
        )
    )
    # components are only published by decile: split the top decile like DPI is split
    top_split = np.array(
        [dpi["90-100%"] - dpi["Top 5%"], dpi["Top 5%"] - dpi["Top 1%"], dpi["Top 1%"]]
    )
    top_split = top_split / top_split.sum()

    def shares(frame):
        q = dict(zip(frame["Quantile or Summary Metric"], frame["Value"]))
        s = [q[g] for g in HH_GROUPS[:9]]
        if "Top 5%" in q and "Top 1%" in q:
            s += [q["90-100%"] - q["Top 5%"], q["Top 5%"] - q["Top 1%"], q["Top 1%"]]
        else:
            s += list(q["90-100%"] * top_split)
        s = np.clip(np.array(s, dtype=float), 0, None)
        return s / s.sum()

    concepts = {
        "comp": "Compensation of employees",
        "contrib": "Contributions for government social insurance",
        "benefits": "Government social benefits",
        "div": "Household dividend income",
        "interest": "Household interest income",
        "prop": "Proprietors' income",
        "taxes": "Taxes",
    }
    out = {}
    for k, prefix in concepts.items():
        out[k] = shares(d[d["Income Concept"].str.startswith(prefix)])
    out["dpi"] = shares(d[d["Income Concept"] == "Disposable personal income"])
    j = pd.read_excel(RAW / "joint_dist.xlsx", sheet_name="shares of NIPA totals")
    joint_year = max(y for y in j.Year.unique() if y <= YEAR)
    SOURCE_YEARS["joint DPI-PCE distribution"] = int(joint_year)
    j = j[
        (j.Year == joint_year)
        & (j.Ranking == "Equivalized Disposable Personal Income")
        & (j.Series == "Personal Consumption Expenditures")
    ]
    j = j.rename(
        columns={
            "Quantile or Summary Metric": "Quantile or Summary Metric",
            "Value": "Value",
        }
    )
    j["Value"] = pd.to_numeric(j["Value"], errors="coerce")
    out["pce"] = shares(j)
    return out


# ---- Model -----------------------------------------------------------------------------
def load_fixed_assets():
    """2017 investment by BEA fixed-asset industry x category x asset type, and residential by tenure."""
    # the BEA workbook is slow to parse, so cache the few numbers we need
    cache = RAW / "fa" / f"fa_{YEAR}_assets_cache.json"
    if cache.exists():
        c = json.loads(cache.read_text())
        return c["inv"], c["res"]
    codes = sorted({p for key in FA_PREFIXES for p in key.split("+")})
    # sheets declare huge dimensions, so read a bounded block with openpyxl instead of pandas
    wb = openpyxl.load_workbook(
        RAW / "fa" / "detailnonres_inv1.xlsx", read_only=True, data_only=True
    )
    raw = {}
    for code in codes:
        rows = list(
            wb[code].iter_rows(min_row=1, max_row=110, max_col=130, values_only=True)
        )
        header = next(
            r for r in rows if r[0] is not None and str(r[0]).strip() == "Asset Codes"
        )
        col = [str(v).split(".")[0] if v is not None else "" for v in header].index(
            str(YEAR)
        )
        assets = {cat: {} for cat in FA_CATEGORIES}
        current = None
        for r in rows:
            label = str(r[0]).strip() if r[0] is not None else ""
            if label in FA_CATEGORIES:
                current = label
            elif current and label and isinstance(r[col], (int, float)):
                assets[current][label] = float(r[col])
        raw[code] = assets
    wb.close()
    inv = {}
    for key in FA_PREFIXES:
        inv[key] = {cat: defaultdict(float) for cat in FA_CATEGORIES}
        for p in key.split("+"):
            for cat in FA_CATEGORIES:
                for a, v in raw[p][cat].items():
                    inv[key][cat][a] += v

    r = pd.read_excel(
        RAW / "fa" / "detailresidential.xlsx", sheet_name="Investment ", header=None
    )
    col = list(r.iloc[5]).index(float(YEAR))
    labels = r.iloc[:, 1].astype(str).str.strip()
    vals = pd.to_numeric(r.iloc[:, col], errors="coerce")
    total = float(vals[labels == "RESIDENTIAL TOTALS"].iloc[0])
    owner = float(vals[labels.str.startswith("Owner-occupied")].sum())
    res = {"total": total, "owner": owner}
    cache.write_text(json.dumps({"inv": inv, "res": res}))
    return inv, res


def category(code):
    for cat, prefixes in CATEGORY_PREFIXES:
        if any(code.startswith(p) for p in prefixes):
            return cat
    return "other"


def ras(X, row_targets, col_targets, iters=500):
    X = X.copy()
    for _ in range(iters):
        r = X.sum(1)
        X *= np.divide(row_targets, r, out=np.zeros_like(r), where=r > 0)[:, None]
        c = X.sum(0)
        X *= np.divide(col_targets, c, out=np.zeros_like(c), where=c > 0)[None, :]
        if np.allclose(X.sum(1), row_targets, rtol=1e-6):
            break
    return X


def gov_level(code):
    if code in ("S00500", "S00600", "S00101", "S00102", "491000"):
        return "federal"
    if code.startswith("GSL") or code in ("S00201", "S00202", "S00203"):
        return "state_local"
    return None


SECTOR_MAP = {
    "11": "Industrial",
    "21": "Industrial",
    "22": "Industrial",
    "23": "Industrial",
    "31G": "Industrial",
    "42": "Trade & transport",
    "44RT": "Trade & transport",
    "48TW": "Trade & transport",
    "FIRE": "Financial & real estate",
    "51": "Services",
    "PROF": "Services",
    "6": "Services",
    "7": "Services",
    "81": "Services",
}


def main():
    global YEAR
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--industry-nodes",
        type=int,
        default=150,
        help="target number of industry nodes (max ~400 = full BEA detail)",
    )
    ap.add_argument("--year", type=int, default=YEAR, choices=sorted(FED_FINANCIALS))
    args = ap.parse_args()
    YEAR = args.year

    nipa = load_nipa()
    hier = load_hierarchy()
    use = load_use()
    inds, coms, fd = use["industries"], use["commodities"], use["fd"]
    Z, F, VA = use["Z"], use["F"], use["VA"]
    make = load_make(inds, coms)
    hh = load_household_shares()
    z1 = load_z1()

    scale_points = []
    if YEAR != IO_DETAIL_YEAR:
        io_year = min(YEAR, SUMMARY_LAST)
        su, sm = load_summary("Use", io_year), load_summary("Make", io_year)
        if YEAR > SUMMARY_LAST:
            su, sm = project_summary(su, sm)
        summ = lambda c: hier.get(c, {}).get("summary", c)  # noqa: E731
        va_codes = ["V00100", "V00200", "V00300"]
        Z, l1, t1 = scale_blocks(Z, coms, inds, summ, summ, su)
        F, l2, t2 = scale_blocks(F, coms, fd, summ, lambda c: c[:4], su)
        VAm, l3, t3 = scale_blocks(
            np.vstack([VA[c] for c in va_codes]),
            va_codes,
            inds,
            lambda c: c[:4],
            summ,
            su,
        )
        make, l4, t4 = scale_blocks(make, inds, coms, summ, summ, sm)
        VA = {c: VAm[k] for k, c in enumerate(va_codes)}
        VA["T008"] = Z.sum(0) + VAm.sum(0)
        lost = (l1 + l2 + l3 + l4) / (t1 + t2 + t3 + t4)
        scale_points = [
            f"BEA publishes the detailed table only for {IO_DETAIL_YEAR}. For later years, BEA "
            "publishes only summary tables (approximately 71 industries).",
            f"The model divides the {IO_DETAIL_YEAR} detailed table into blocks. Each block agrees "
            "with one cell of the summary table.",
            f"The model scales each block so that its sum is equal to the {YEAR} summary value.",
            f"Thus, the totals for industries, value added, final demand and imports are {YEAR} "
            f"values. Only the split inside each block comes from {IO_DETAIL_YEAR}.",
            f"Some summary cells have no {IO_DETAIL_YEAR} detail. The model does not use these "
            f"cells ({lost:.2%} of the table value).",
        ]
        if YEAR > SUMMARY_LAST:
            SOURCE_YEARS["input-output structure (summary)"] = SUMMARY_LAST
            scale_points += [
                f"PROJECTION: BEA summary tables stop at {SUMMARY_LAST}. Thus, the {YEAR} summary "
                f"tables are a projection from the {SUMMARY_LAST} tables.",
                "All scale factors come from published BEA data. No scale factor is a hand-entered "
                "number.",
                "The model scales the purchases of each industry by the growth of its gross output "
                "minus its value added. Source: BEA GDP by Industry, tables TGO105 and TVA105.",
                "The model sets the wages, taxes on production and operating surplus of each "
                f"industry to the BEA {YEAR} values. Source: BEA tables TVA105 and TVA112.",
                "BEA does not show some value-added parts for some industries. For these "
                "industries, the model scales each part by the growth of total value added.",
                "The model scales each final-demand column by the growth of its NIPA total.",
                "These columns are: consumer spending, equipment, intellectual property, "
                "nonresidential structures, housing, inventories, exports, imports, federal "
                "defense, federal nondefense, and state and local government.",
                "The model scales the output of each industry in the Make table by the growth of its "
                "gross output.",
                f"The product mix of each purchase column stays at {SUMMARY_LAST} shares.",
                f"The detail inside each summary cell stays at {IO_DETAIL_YEAR} shares. Thus, two "
                "detailed industries in the same summary group grow at the same rate.",
                "The model does not balance the projected table again. The bank-credit and "
                "net-saving nodes get the differences.",
            ]
    SOURCE_YEARS["input-output structure (detail)"] = IO_DETAIL_YEAR

    assume(
        "Base year and data sources",
        [
            f"This model shows the money flows for one year: {YEAR}.",
            "All values are in billions of US dollars ($B) for each year.",
            "The flows between industries come from the BEA Use and Make tables. These tables use "
            "producer prices, before redefinitions.",
            "Government, tax, transfer and income totals come from NIPA tables 1.12, 2.1, 3.2, 3.3, "
            "3.12 and 4.1.",
            "Pension cash flows come from NIPA table 7.20 (all plans) and table 7.23 (federal plans).",
            "Investment by industry and by asset type comes from the BEA Fixed Assets detailed "
            "estimates.",
            "Household groups come from the BEA Distribution of Personal Income and the BEA/BLS "
            "joint DPI-PCE distribution.",
            "Deposit balances come from the Federal Reserve Financial Accounts (Z.1) and "
            "Distributional Financial Accounts (DFA).",
            "BEA tables have different release dates. Thus, some totals can be different by a few "
            "percent.",
        ],
    )
    dpi_year = SOURCE_YEARS["distribution of personal income"]
    joint_year = SOURCE_YEARS["joint DPI-PCE distribution"]
    fed_audited = "tsy" in FED_FINANCIALS[YEAR]
    assume(
        "Data years and scaling",
        [
            "Data years in this build: "
            + ", ".join(
                f"{k} {v}" for k, v in {**SOURCE_YEARS, "all other data": YEAR}.items()
            )
            + ".",
            (
                f"For {YEAR}, the model uses the BEA detailed table directly. No scaling is "
                "necessary."
                if YEAR == IO_DETAIL_YEAR
                else None
            ),
            *scale_points,
            f"Taxes, benefits, income and pension totals come from {YEAR} NIPA data.",
            f"Deposit balances come from {YEAR} Z.1 and DFA data.",
            f"Investment by industry and asset type comes from {YEAR} Fixed Assets data.",
            f"Income shares by household group come from {dpi_year} data."
            + (
                " This is the latest year that BEA publishes."
                if dpi_year < YEAR
                else ""
            ),
            (
                f"For {dpi_year}, BEA gives the top 1% and top 5% shares as ranges. The model uses "
                "the middle of each range."
                if NOTES.get("dpi_ranges")
                else None
            ),
            f"Spending shares by household group come from {joint_year} data."
            + (
                " This is the latest year that BEA publishes."
                if joint_year < YEAR
                else ""
            ),
            (
                f"Federal Reserve figures come from the audited Federal Reserve Banks financial "
                f"statements for {YEAR}. The values are copied exactly."
                if fed_audited
                else f"Federal Reserve figures for {YEAR} come from the Federal Reserve Board press "
                "release. The model divides securities income between Treasury and mortgage-backed "
                "securities by the Z.1 holdings."
            ),
        ],
    )

    # Drop imputed / non-monetary items
    drop = {"531HSO", "S00401", "S00402", "S00900"}
    com_idx = {c: i for i, c in enumerate(coms)}
    fdi = {c: j for j, c in enumerate(fd)}
    ooh = F[com_idx["531HSO"], fdi["F01000"]]
    assume(
        "Imputations removed",
        [
            "The model removes owner-occupied housing (code 531HSO).",
            'This item is the rent that home owners "pay to themselves". It is '
            f"${ooh/1e3:,.0f}B of consumer spending, but no money moves.",
            "The model also removes scrap, used goods and the rest-of-world adjustment rows.",
            "The model does not pay out the rental income of persons, because most of it is this "
            "imputed rent. The real-estate operating surplus stays with the industry.",
            "The model does not remove other BEA imputations. Examples are imputed financial "
            "services and benefits in kind from employers.",
        ],
    )
    for c in drop:
        if c in com_idx:
            Z[com_idx[c], :] = 0
            F[com_idx[c], :] = 0
    ind_keep = [i for i, c in enumerate(inds) if c not in drop]

    # Commodity -> producing industry shares (make table)
    make_tot = make.sum(0)
    D = np.divide(make, make_tot, out=np.zeros_like(make), where=make_tot > 0)
    for c in drop:
        if c in inds:
            D[inds.index(c), :] = 0
    assume(
        "Who gets paid for a commodity",
        [
            "When a buyer purchases a commodity, the model removes the imported part first.",
            "The model pays the domestic part to each industry in proportion to its production of "
            "that commodity. Source: BEA Make table (market shares).",
        ],
    )

    imports = np.clip(-F[:, fdi["F05000"]], 0, None)
    exports = np.clip(F[:, fdi["F04000"]], 0, None)
    pos_uses = Z.sum(1) + np.clip(F, 0, None).sum(1) - exports
    mshare = np.clip(
        np.divide(imports, pos_uses, out=np.zeros_like(imports), where=pos_uses > 0),
        0,
        1,
    )
    if "S00300" in com_idx:
        mshare[com_idx["S00300"]] = 1.0
    assume(
        "Imports",
        [
            "For each commodity, the import share is equal to imports divided by domestic use. "
            "Domestic use does not include exports.",
            "Each domestic buyer of the commodity sends that share of its payment to the Rest of "
            "World (RoW).",
            "Domestic buyers are industries, households, governments and investors.",
            "All exports are made in the US.",
            "Noncomparable imports are 100% imported.",
        ],
    )

    flows = defaultdict(lambda: defaultdict(float))

    def add(src, dst, typ, amt):
        if amt > 0 and src != dst:
            flows[(src, dst)][typ] += amt

    def buy(buyer, com_vec, typ):
        """Buyer pays vector of commodity purchases -> RoW + producing industries."""
        add(buyer, "ROW_SINK", "imports", float((com_vec * mshare).sum()))
        dom = com_vec * (1 - mshare)
        to_ind = D @ dom
        for a in np.nonzero(to_ind > 0)[0]:
            add(buyer, "I:" + inds[a], typ, float(to_ind[a]))

    # Intermediate purchases
    for j in ind_keep:
        buy("I:" + inds[j], Z[:, j], "intermediate purchases")

    def find(name_part):
        for c in inds:
            if name_part.lower() in hier.get(c, {}).get("name", "").lower():
                return c
        raise SystemExit(f"industry not found: {name_part}")

    funds_code = find("Funds, trusts")
    insurer_code = find("Insurance carriers, except direct life")
    bank_code = find("Monetary authorities")
    nonbank_code = find("Nondepository credit")
    comp_io_total = float(sum(VA["V00100"][j] for j in ind_keep))
    comp_fed = float(
        sum(VA["V00100"][j] for j in ind_keep if gov_level(inds[j]) == "federal")
    )
    comp_nonfed = comp_io_total - comp_fed
    # NIPA's (negative) imputed DB contributions are non-cash, so use actual contributions only
    employer_insurance = nipa["emp_pension"] - (nipa["pen_emp"] + nipa["pen_imp_emp"])
    pen_emp_nonfed = nipa["pen_emp"] - nipa["fpen_emp"]
    pen_hh_nonfed = nipa["pen_hh"] - nipa["fpen_hh"]
    pen_int_nonfed = nipa["pen_mint"] - nipa["fpen_mint"]
    pen_ben_nonfed = nipa["pen_benefits"] - nipa["fpen_benefits"]

    # Health: Medicare/Medicaid and employer-funded insurers pay providers directly
    health = np.array([c.startswith(("621", "622", "623")) for c in coms])
    pce = np.clip(F[:, fdi["F01000"]], 0, None)
    health_pce = pce[health].sum()
    gov_health = min(nipa["medicare"] + nipa["medicaid"], GOV_HEALTH_CAP * health_pce)
    g = gov_health / health_pce
    ins_claims = min(employer_insurance, 0.95 * health_pce - gov_health)
    gi = ins_claims / health_pce
    fed_frac = nipa["medicare"] / (nipa["medicare"] + nipa["medicaid"])
    gov_health_vec = np.where(health, pce * g, 0.0)
    ins_vec = np.where(health, pce * gi, 0.0)
    buy("FED_SRC", gov_health_vec * fed_frac, "Medicare (paid to providers)")
    buy("SL", gov_health_vec * (1 - fed_frac), "Medicaid (paid to providers)")
    buy("I:" + insurer_code, ins_vec, "insurance claims paid to providers")
    pce_hh = pce - gov_health_vec - ins_vec
    assume(
        "Medicare, Medicaid and private health insurance",
        [
            f'NIPA records Medicare (${nipa["medicare"]/1e3:.0f}B) and Medicaid '
            f'(${nipa["medicaid"]/1e3:.0f}B) as transfers to persons. But the money goes to health '
            "providers.",
            "In this model, the Federal government pays Medicare directly to providers. State and "
            "local governments pay Medicaid directly to providers.",
            "Providers are ambulatory care, hospitals and nursing facilities (NAICS 621-623).",
            f"Government payments cover {g:.0%} of consumer spending on these services.",
            "Employer health-insurance contributions go to insurers. Insurers pay claims to "
            f"providers for a further {gi:.0%}.",
            "Households pay the remainder directly.",
            "The model removes the government amounts from household benefits.",
        ],
    )

    # Households: PCE split across income groups with tilts + RAS
    H = len(HH_GROUPS)
    hh_ids = ["HH:" + g_ for g_ in HH_GROUPS]
    cats = [category(c) for c in coms]
    tilt = np.array(
        [[TILTS[cats[i]][HH_QUINTILE[h]] for h in range(H)] for i in range(len(coms))]
    )
    seed = tilt * pce_hh[:, None] * hh["pce"][None, :]
    hh_pce_total = hh["pce"] * pce_hh.sum()
    X = ras(seed, pce_hh, hh_pce_total)
    for h in range(H):
        buy(hh_ids[h], X[:, h], "consumer purchases")
    assume(
        "Household spending mix",
        [
            "The total consumer spending of each household group follows the BEA/BLS joint "
            f"DPI-PCE distribution ({joint_year}).",
            "The mix of purchases of each group starts from hand-entered tilts by income quintile.",
            "These tilts are approximate. They use the BLS Consumer Expenditure Survey 2017 "
            "budget shares.",
            "For example, the bottom quintile spends approximately 1.6 times the average share on "
            "rent. The top quintile spends approximately 1.35 times the average share on travel.",
            "A RAS balance then makes the commodity totals agree exactly with BEA consumer spending.",
            "The model sorts commodities into spending categories by NAICS prefix.",
            "All groups in the top 10% use the tilt of the top quintile.",
        ],
    )
    assume(
        "Household groups",
        [
            "Households are in 12 groups. BEA ranks them by equivalized disposable personal "
            "income (DPI).",
            "The groups are deciles 1 to 9, 90-95%, 95-99% and the top 1%.",
            "BEA publishes income parts (wages, dividends, taxes and other parts) only by decile.",
            "Thus, the model divides the top decile with the DPI split. This gives a top 1% share "
            "of capital income that is too low.",
            "BEA does not publish a split by household type (for example, with or without "
            "children). The model does not show household type.",
        ],
    )

    # Government purchases
    fed_cols = [c for c in fd if c.startswith(("F06", "F07"))]
    sl_cols = [c for c in fd if c.startswith("F10")]
    buy(
        "FED_SRC",
        sum(np.clip(F[:, fdi[c]], 0, None) for c in fed_cols),
        "federal purchases",
    )
    buy(
        "SL",
        sum(np.clip(F[:, fdi[c]], 0, None) for c in sl_cols),
        "state & local purchases",
    )
    # ---- Investment: each industry buys its own capital goods (BEA Fixed Assets shares)
    fa_inv, fa_res = load_fixed_assets()
    out_vec = VA["T008"]
    prefix_list = [(p, fa) for fa, ps in FA_PREFIXES.items() for p in ps]
    fa_members = defaultdict(list)
    unmatched = []
    for j in ind_keep:
        c = inds[j]
        if gov_level(c):
            continue
        match = max(
            ((p, fa) for p, fa in prefix_list if c.startswith(p)),
            key=lambda t: len(t[0]),
            default=None,
        )
        if match:
            fa_members[match[1]].append(j)
        else:
            unmatched.append(c)
    cat_label = {
        "EQUIPMENT": "equipment",
        "STRUCTURES": "structures",
        "IPP": "software & R&D",
    }
    groups = list(fa_members)
    bridge_gap = {}
    for cat, col in FA_CATEGORIES.items():
        vec = np.clip(F[:, fdi[col]], 0, None)
        assets = sorted(
            {a for fa in groups for a, v in fa_inv[fa][cat].items() if v > 0}
        )
        A = np.array(
            [sum(fa_inv[fa][cat].get(a, 0.0) for fa in groups) for a in assets]
        )
        mask = np.zeros((len(coms), len(assets)), dtype=bool)
        for k, a in enumerate(assets):
            prefixes = tuple(asset_commodities(a))
            if prefixes:
                mask[:, k] = [c.startswith(prefixes) for c in coms]
        present = vec > 0
        mask[present & ~mask.any(1), :] = True
        mask[:, ~(mask & present[:, None]).any(0)] = present[:, None]
        seed = np.where(mask, vec[:, None] * A[None, :], 0.0)
        # B[c, k] = $ of commodity c bought as asset type k; matches IO and fixed-asset totals
        B = ras(seed, vec, A * vec.sum() / A.sum())
        row = B.sum(1)
        B *= np.divide(vec, row, out=np.zeros_like(row), where=row > 0)[:, None]
        bridge_gap[cat] = float(
            np.abs(B.sum(0) - A * vec.sum() / A.sum()).sum() / vec.sum()
        )
        for fa, members in fa_members.items():
            w = np.array([fa_inv[fa][cat].get(a, 0.0) for a in assets]) / A
            fa_vec = B @ w
            mem_out = sum(out_vec[j] for j in members)
            for j in members:
                buy(
                    "I:" + inds[j],
                    fa_vec * out_vec[j] / mem_out,
                    f"investment: {cat_label[cat]}",
                )

    res_vec = np.clip(F[:, fdi["F02R00"]], 0, None)
    owner_share = fa_res["owner"] / fa_res["total"]
    for h in range(H):
        buy(
            hh_ids[h],
            res_vec * owner_share * hh["comp"][h],
            "new homes & renovations (owner-occupied)",
        )
    buy(
        "I:531HST",
        res_vec * (1 - owner_share),
        "rental housing construction (tenant-occupied)",
    )

    inv_chg = np.clip(F[:, fdi["F03000"]], 0, None)
    holders = [
        j
        for j in ind_keep
        if hier.get(inds[j], {}).get("sector") in ("11", "21", "31G", "42", "44RT")
    ]
    holders_out = sum(out_vec[j] for j in holders)
    for j in holders:
        buy("I:" + inds[j], inv_chg * out_vec[j] / holders_out, "inventory build-up")

    for i in np.nonzero(exports > 0)[0]:
        to_ind = D[:, i] * exports[i]
        for a in np.nonzero(to_ind > 0)[0]:
            add("ROW_SRC", "I:" + inds[a], "export sales", float(to_ind[a]))
    assume(
        "Government purchases",
        [
            "The Federal spending node pays for federal consumption and gross investment "
            "(defense and nondefense).",
            'This includes purchases from the "Federal general government" industries. These '
            "industries then pay federal employees.",
            "State and local purchases use the same method.",
            "Government purchases use producer prices.",
        ],
    )
    assume(
        "Investment",
        [
            "Each industry pays for its own capital goods. The model does not use an investment "
            "pool.",
            f"BEA Fixed Assets detailed estimates for {YEAR} give private nonresidential "
            f"investment for {len(fa_members)} fixed-asset industries and approximately 95 asset "
            "types.",
            "Examples of asset types are manufacturing structures, mining and oilfield machinery, "
            "light trucks, prepackaged software and R&D.",
            "A hand-made concordance by NAICS code connects each asset type to IO commodities. "
            "For example, all R&D goes to commodity 541700.",
            "All asset types share the trade and transport margins, installation costs and broker "
            "fees.",
            "A RAS balance makes this bridge agree exactly with the IO investment columns "
            "(F02E00, F02S00, F02N00).",
            "The remaining difference from the fixed-asset totals is: "
            + ", ".join(f"{k.lower()} {v:.1%}" for k, v in bridge_gap.items())
            + ".",
            "In each fixed-asset industry, the model divides the amount among its IO detail "
            "industries by gross output.",
            "The model divides residential investment by tenure, from the BEA residential detail. "
            f"{owner_share:.0%} is owner-occupied.",
            "Household groups pay for owner-occupied homes in proportion to their wage shares. This "
            "is a proxy for who buys and repairs homes.",
            "The tenant-occupied housing industry pays for rental housing.",
            "Goods-producing and trade industries pay for inventory increases in proportion to "
            "their output. The model ignores inventory decreases.",
            "Retained earnings stay with each industry. If an industry invests more than it keeps, "
            "it uses bank credit. If it invests less, it saves.",
            "Purchases that an industry makes from itself are internal. The model does not show "
            "them.",
            (
                f"Industries without fixed-asset data: {', '.join(unmatched)}."
                if unmatched
                else None
            ),
        ],
    )

    # Value added
    comp, tpi, gos, output = VA["V00100"], VA["V00200"], VA["V00300"], VA["T008"]
    w_frac = nipa["wages"] / nipa["comp"]
    si_frac = nipa["emp_si"] / nipa["comp"]
    tpi_fed_frac = nipa["fed_tpi"] / (nipa["fed_tpi"] + nipa["sl_tpi"])

    private = [j for j in ind_keep if gov_level(inds[j]) is None]
    gos_priv = np.clip(gos, 0, None)
    gos_priv_tot = sum(gos_priv[j] for j in private)
    fedfin = FED_FINANCIALS[YEAR]
    if "tsy" in fedfin:
        sec_income = fedfin["tsy"] + fedfin["mbs"]
        tsy_share = fedfin["tsy"] / sec_income
        split_src = "values as reported"
    else:
        sec_income = fedfin["securities_income"]
        tsy_share = z1["fed_tsy"] / (z1["fed_tsy"] + z1["fed_agency"])
        split_src = "divided by Z.1 holdings"
    fed_tsy_int = sec_income * tsy_share
    fed_mbs_int = sec_income - fed_tsy_int
    fed_int_to_frb = fed_tsy_int
    fed_int_res = nipa["fed_interest"] - nipa["fed_int_row"]
    # interest the Treasury pays its own employee pension funds is intra-federal: netted out
    fed_int_persons = fed_int_res - fed_int_to_frb - nipa["fpen_mint"]
    # Fed reverse-repo interest reaches households via money market funds; it is inside personal interest
    ind_interest = (
        nipa["pers_interest"]
        - (fed_int_res - fed_int_to_frb)
        - nipa["sl_interest"]
        - nipa["pen_imp_int"]
        - fedfin["int_repo"]
    )
    payouts = {
        "corporate income tax (federal)": ("FED_SINK", nipa["fed_corp"]),
        "corporate income tax (state & local)": ("SL", nipa["sl_corp"]),
        "dividends": ("div", nipa["pers_div"] - nipa["pen_div"]),
        "dividends to pension funds": ("PEN_SINK", nipa["pen_div"]),
        "interest paid to households": ("interest", ind_interest - pen_int_nonfed),
        "interest to pension funds": ("PEN_SINK", pen_int_nonfed),
        "proprietors' income": ("prop", nipa["proprietors"]),
    }
    payout_total = sum(v for _, v in payouts.values())
    if payout_total >= gos_priv_tot:
        raise SystemExit("payouts exceed GOS")

    for j in ind_keep:
        src = "I:" + inds[j]
        for h in range(H):
            add(src, hh_ids[h], "wages & salaries", comp[j] * w_frac * hh["comp"][h])
        add(
            src,
            "FED_SINK",
            "employer social insurance contributions",
            comp[j] * si_frac,
        )
        if gov_level(inds[j]) == "federal":
            add(
                src,
                "FED_SINK",
                "employer contributions to federal employee pensions",
                nipa["fpen_emp"] * comp[j] / comp_fed,
            )
        else:
            add(
                src,
                "PEN_SINK",
                "employer pension contributions",
                pen_emp_nonfed * comp[j] / comp_nonfed,
            )
        add(
            src,
            "I:" + insurer_code,
            "employer health/insurance contributions",
            employer_insurance * comp[j] / comp_io_total,
        )
        t = max(tpi[j], 0)
        add(src, "FED_SINK", "taxes on production & imports", t * tpi_fed_frac)
        add(src, "SL", "taxes on production & imports", t * (1 - tpi_fed_frac))
        lvl = gov_level(inds[j])
        if lvl:
            add(
                src,
                "FED_SINK" if lvl == "federal" else "SL",
                "govt enterprise surplus / depreciation",
                gos_priv[j],
            )
            continue
        share = gos_priv[j] / gos_priv_tot
        for typ, (dst, total) in payouts.items():
            amt = total * share
            if dst in ("FED_SINK", "SL", "PEN_SINK"):
                add(src, dst, typ, amt)
            else:
                for h in range(H):
                    add(src, hh_ids[h], typ, amt * hh[dst][h])
    assume(
        "Paying out value added",
        [
            f"The model divides compensation with NIPA {YEAR} ratios.",
            f"{w_frac:.1%} is wages and salaries. These go to households by BEA compensation decile "
            "shares.",
            f"{si_frac:.1%} is employer social-insurance contributions. These go directly to the "
            "Federal government.",
            "Employer pension contributions go to the pension sink (refer to Pensions).",
            f"Employer health and insurance contributions (${employer_insurance/1e3:.0f}B) go to "
            "insurers. This is NIPA employer pension and insurance contributions minus cash pension "
            "contributions.",
            "Each industry pays these items in proportion to its compensation.",
            "The cash paid is a little more than BEA compensation. The reason is that the model does "
            "not include the negative imputed pension contributions of NIPA.",
            "The credit node covers this difference.",
            f"{tpi_fed_frac:.1%} of taxes on production goes to the Federal government. The "
            "remainder goes to state and local governments.",
            "This NIPA ratio is the same for all industries. The model ignores subsidies.",
        ],
    )
    assume(
        "Paying out operating surplus",
        [
            f"Private gross operating surplus is ${gos_priv_tot/1e3:,.0f}B, after the model "
            "removes owner-occupied housing.",
            "The model divides it in the SAME proportions for all industries, as follows.",
            f'Corporate income taxes: federal ${nipa["fed_corp"]/1e3:.0f}B, state and local '
            f'${nipa["sl_corp"]/1e3:.0f}B.',
            f'Dividends: ${nipa["pers_div"]/1e3:.0f}B. Of this, ${nipa["pen_div"]/1e3:.0f}B goes '
            "to pension funds.",
            f"Cash interest: ${ind_interest/1e3:.0f}B. Of this, ${pen_int_nonfed/1e3:.0f}B goes to "
            "pension funds.",
            "Cash interest is personal interest income minus the interest that governments pay. It "
            f'does not include ${nipa["pen_imp_int"]/1e3:.0f}B of non-cash imputed interest on '
            "pension claims.",
            f'Proprietors\' income: ${nipa["proprietors"]/1e3:.0f}B.',
            f"The remainder (${(gos_priv_tot-payout_total)/1e3:,.0f}B) stays with the industry. The "
            "industry uses it for its own investment.",
            "Each income type goes to households by its own BEA decile shares.",
            "In reality, payout ratios are very different between industries. This is a large "
            "simplification.",
            "The model does not show cross-border dividends or interest, except interest on federal "
            "debt.",
        ],
    )
    assume(
        "Government industries",
        [
            "Government industries and enterprises return their operating surplus to the "
            "government that owns them. Most of this surplus is depreciation.",
            "Federal: general government, Postal Service and federal enterprises.",
            "State and local: education, hospitals, other services and enterprises.",
        ],
    )

    # Households: income from govt, outflows to govt
    ben_fed = nipa["fed_benefits"] - nipa["medicare"]
    ben_sl = nipa["sl_benefits"] - nipa["medicaid"]
    emp_contrib = nipa["contrib_total"] - nipa["emp_si"]
    for h in range(H):
        hid = hh_ids[h]
        add(
            "FED_SRC",
            hid,
            "social benefits (Social Security, SNAP, tax credits...)",
            ben_fed * hh["benefits"][h],
        )
        add("SL", hid, "social benefits (state & local)", ben_sl * hh["benefits"][h])
        add(
            "FED_SRC",
            hid,
            "interest on Treasury debt",
            fed_int_persons * hh["interest"][h],
        )
        add(
            "SL",
            hid,
            "interest on municipal debt",
            nipa["sl_interest"] * hh["interest"][h],
        )
        add(
            hid,
            "FED_SINK",
            "federal personal income taxes",
            nipa["fed_personal_taxes"] * hh["taxes"][h],
        )
        add(
            hid,
            "SL",
            "state & local personal taxes",
            nipa["sl_personal_taxes"] * hh["taxes"][h],
        )
        add(
            hid,
            "FED_SINK",
            "employee social insurance contributions",
            emp_contrib * hh["contrib"][h],
        )
        add(
            hid,
            "I:" + bank_code,
            "interest on household debt",
            nipa["hh_interest_paid"] * hh["pce"][h] * HH_INTEREST_DEPOSITORY,
        )
        add(
            hid,
            "I:" + nonbank_code,
            "interest on household debt",
            nipa["hh_interest_paid"] * hh["pce"][h] * (1 - HH_INTEREST_DEPOSITORY),
        )
        add(
            hid,
            "SL",
            "fees, fines & transfers to govt",
            nipa["hh_transfers_gov"] * hh["taxes"][h],
        )
        add(
            hid,
            "ROW_SINK",
            "remittances abroad",
            nipa["hh_transfers_row"] * hh["comp"][h],
        )
        add(
            hid,
            "PEN_SINK",
            "employee pension contributions (401k etc.)",
            pen_hh_nonfed * hh["comp"][h],
        )
        add(
            "PEN_SRC",
            hid,
            "pension benefits & withdrawals",
            pen_ben_nonfed * hh["benefits"][h],
        )
        add(
            "FED_SRC",
            hid,
            "federal employee & military pensions",
            nipa["fpen_benefits"] * hh["benefits"][h],
        )
    add(
        "PEN_SRC",
        "I:" + funds_code,
        "pension plan administrative fees",
        nipa["pen_admin"],
    )
    assume(
        "Pensions",
        [
            "Pension funds keep money for the future. Thus, the model divides them into a SINK and "
            "a SOURCE, as for the Federal government and RoW.",
            "Cash flows come from NIPA table 7.20 (all defined-benefit and defined-contribution "
            "plans).",
            "Federal employee plans (NIPA table 7.23) are part of the Federal government.",
            f"Into the sink: employer contributions of ${pen_emp_nonfed/1e3:.0f}B, by industry "
            "compensation.",
            f"Into the sink: employee contributions of ${pen_hh_nonfed/1e3:.0f}B, by household "
            "wage shares.",
            f'Into the sink: dividends of ${nipa["pen_div"]/1e3:.0f}B and monetary interest of '
            f"${pen_int_nonfed/1e3:.0f}B, from industries by operating-surplus share.",
            f"Out of the source: benefits and withdrawals of ${pen_ben_nonfed/1e3:.0f}B to "
            "households.",
            "These use the shares of government social benefits, as a proxy for where retirees "
            "are.",
            f'Out of the source: administrative fees of ${nipa["pen_admin"]/1e3:.0f}B to the '
            '"Funds, trusts & other financial vehicles" industry.',
            f'Federal agency contributions (${nipa["fpen_emp"]/1e3:.0f}B) go to the Federal sink. '
            f'Federal pensions (${nipa["fpen_benefits"]/1e3:.0f}B) come from the Federal source.',
            "The model removes the Treasury interest that goes to its own pension funds.",
            "Pension-fund purchases and sales of existing securities are swaps between savers. "
            "The model does not show them.",
        ],
    )
    assume(
        "Taxes and transfers by household group",
        [
            'Personal income taxes (federal, state and local) use the BEA "Taxes" decile shares.',
            'Employee social-insurance contributions use the BEA "Contributions" shares.',
            "Social benefits (not Medicare or Medicaid) use the BEA "
            '"Government social benefits" shares.',
            "Interest on government debt uses household interest-income shares. The model puts "
            "ownership through mutual funds onto households.",
            "Household interest payments follow consumer-spending shares.",
            f"{HH_INTEREST_DEPOSITORY:.0%} of household interest payments goes to banks. The "
            "remainder goes to non-bank lenders.",
            "Remittances abroad follow wage shares.",
        ],
    )

    # Federal government, Fed, RoW
    add("FED_SRC", "SL", "grants-in-aid to states & localities", nipa["grants"])
    add("FED_SRC", "FRB", "interest on Treasuries held by the Fed", fed_tsy_int)
    add(
        "I:" + bank_code,
        "FRB",
        "mortgage-backed securities interest passed to the Fed",
        fed_mbs_int,
    )
    add("FRB", "FED_SINK", "Fed remittances to Treasury", nipa["fed_frb_remit"])
    add("FRB", "I:" + bank_code, "interest on reserve balances", fedfin["int_reserves"])
    for h in range(H):
        add(
            "FRB",
            hh_ids[h],
            "Fed reverse-repo interest (via money market funds)",
            fedfin["int_repo"] * hh["interest"][h],
        )
    fed_loss = (
        fedfin["int_reserves"] + fedfin["int_repo"] + nipa["fed_frb_remit"] - sec_income
    )
    add(
        "FED_SRC", "FRB", "Fed losses funded by new reserves (deferred asset)", fed_loss
    )
    add(
        "FED_SRC", "ROW_SINK", "interest on Treasuries held abroad", nipa["fed_int_row"]
    )
    add(
        "FED_SRC",
        "ROW_SINK",
        "transfers abroad (aid etc.)",
        nipa["fed_transfers_row"] + nipa["fed_other_transfers_row"],
    )
    assume(
        "Money creation and removal (MMT view)",
        [
            "The model divides the Federal government into a SOURCE node and a SINK node.",
            "Federal spending puts credit in bank reserves. This makes new money.",
            "Taxes, fees and Fed remittances remove money.",
            "State and local governments are currency USERS. They are an ordinary node. They must "
            "tax or borrow before they spend.",
            "The model divides the Rest of World into a SOURCE and a SINK. Export receipts bring "
            "dollars in. Import payments send dollars out.",
            'Bank lending is the "horizontal" source of money.',
            'If a node pays out more than it gets, "Bank credit & dis-saving" supplies the '
            'difference. If it gets more than it pays out, the difference goes to "Net saving & '
            'debt repayment".',
            "Pension funds also have a source and a sink (refer to Pensions).",
            "For money creation, the model combines the Federal Reserve with the Treasury.",
            f"In {YEAR}, the Fed got ${sec_income/1e3:.0f}B from its securities: "
            f"{tsy_share:.0%} from Treasury securities and {1-tsy_share:.0%} from mortgage-backed "
            f"securities ({split_src}).",
            "The mortgage-backed part comes from the banking node, because banks collect mortgage "
            "interest.",
            f"The Fed paid ${fedfin['int_reserves']/1e3:.0f}B interest on bank reserves.",
            f"The Fed paid ${fedfin['int_repo']/1e3:.0f}B interest on reverse repos. This goes to "
            "households through money market funds, by interest-income shares.",
            f'The Fed sent ${nipa["fed_frb_remit"]/1e3:.1f}B to the Treasury.',
            (
                f"The Fed had a loss of ${fed_loss/1e3:.0f}B. The Fed records it as a "
                '"deferred asset" and pays it with new reserves. Thus, it comes from the Federal '
                "money-creation node."
                if fed_loss > 0
                else None
            ),
            f"Federal interest: ${fed_int_to_frb/1e3:.0f}B to the Federal Reserve, "
            f'${nipa["fed_int_row"]/1e3:.0f}B abroad and ${fed_int_persons/1e3:.0f}B to '
            "households.",
        ],
    )

    # ---- Aggregate industries into nodes ----------------------------------------------
    priv_out = sorted(ind_keep, key=lambda j: -output[j])
    target = args.industry_nodes

    def grouping(k):
        top = set(priv_out[:k])
        groups = defaultdict(list)
        for j in priv_out[k:]:
            groups[hier.get(inds[j], {}).get("summary", inds[j])].append(j)
        return top, groups

    k = len(priv_out)
    for kk in range(len(priv_out), -1, -1):
        top, groups = grouping(kk)
        if len(top) + len(groups) <= target:
            k = kk
            break
    top, groups = grouping(k)
    ind_node = {}
    node_meta = {}

    def ind_sector(code):
        lvl = gov_level(code)
        if lvl:
            return (
                "Federal government" if lvl == "federal" else "State & local government"
            )
        return SECTOR_MAP.get(hier.get(code, {}).get("sector"), "Services")

    for j in top:
        c = inds[j]
        nid = "I:" + c
        ind_node[nid] = nid
        node_meta[nid] = {
            "name": hier.get(c, {}).get("name", c),
            "codes": [c],
            "sector": ind_sector(c),
            "output": output[j],
        }
    for sm, members in groups.items():
        codes = [inds[j] for j in members]
        if len(members) == 1:
            nid = "I:" + codes[0]
            name = hier.get(codes[0], {}).get("name", codes[0])
        else:
            nid = "G:" + sm
            name = "Other " + hier.get(codes[0], {}).get("summary_name", sm).lower()
        for c in codes:
            ind_node["I:" + c] = nid
        node_meta[nid] = {
            "name": name,
            "codes": codes,
            "sector": ind_sector(codes[0]),
            "output": float(sum(output[j] for j in members)),
        }
    if "I:" + funds_code in node_meta:
        node_meta["I:" + funds_code][
            "name"
        ] = "Funds, trusts & other financial vehicles (mutual funds, trusts, fund admin)"
    assume(
        "Industry nodes",
        [
            f"The model has {len(ind_keep)} BEA detail industries. It shows them as "
            f"{len(node_meta)} nodes.",
            f"The {k} largest industries by gross output each have their own node.",
            'The model merges the other industries in their BEA summary industry ("Other ...").',
            "To change this, run build_flows.py --industry-nodes N (maximum approximately 400).",
            "Purchases between industries in the same node are internal. The model does not show "
            "them.",
        ],
    )

    special = {
        "FED_SRC": (
            "Federal Govt - spending (money creation)",
            "Federal government",
            "source",
        ),
        "FED_SINK": (
            "Federal Govt - taxes (money destruction)",
            "Federal government",
            "sink",
        ),
        "SL": (
            "State & local governments (treasuries)",
            "State & local government",
            "node",
        ),
        "FRB": ("Federal Reserve", "Monetary & credit", "node"),
        "ROW_SRC": (
            "Rest of World - export receipts (inflow)",
            "Rest of world",
            "source",
        ),
        "ROW_SINK": (
            "Rest of World - import payments (outflow)",
            "Rest of world",
            "sink",
        ),
        "CREDIT": (
            "Bank credit & dis-saving (horizontal creation)",
            "Monetary & credit",
            "source",
        ),
        "SAVING": (
            "Net saving & debt repayment (horizontal destruction)",
            "Monetary & credit",
            "sink",
        ),
        "PEN_SRC": (
            "Retirement funds - benefits paid out (pensions)",
            "Pensions",
            "source",
        ),
        "PEN_SINK": (
            "Retirement funds - contributions & investment income (pensions)",
            "Pensions",
            "sink",
        ),
    }
    hh_labels = [
        "Households D1 (bottom 10%)",
        "Households D2",
        "Households D3",
        "Households D4",
        "Households D5",
        "Households D6",
        "Households D7",
        "Households D8",
        "Households D9",
        "Households 90-95%",
        "Households 95-99%",
        "Households top 1%",
    ]
    for hid, lab in zip(hh_ids, hh_labels):
        special[hid] = (lab, "Household", "node")

    agg = defaultdict(lambda: defaultdict(float))
    for (s, t), types in flows.items():
        s2, t2 = ind_node.get(s, s), ind_node.get(t, t)
        if s2 == t2:
            continue
        for typ, v in types.items():
            agg[(s2, t2)][typ] += v

    # prune small edges, keep each node's top 3 outflows
    by_src = defaultdict(list)
    for (s, t), types in agg.items():
        by_src[s].append((sum(types.values()), t))
    keep = set()
    dropped = 0.0
    for s, lst in by_src.items():
        lst.sort(reverse=True)
        for rank, (v, t) in enumerate(lst):
            if v >= MIN_EDGE or rank < 3:
                keep.add((s, t))
            else:
                dropped += v
    agg = {k_: v for k_, v in agg.items() if k_ in keep}

    # balance: residuals to/from credit & saving
    all_nodes = set(node_meta) | set(special)
    inflow = defaultdict(float)
    outflow = defaultdict(float)
    for (s, t), types in agg.items():
        v = sum(types.values())
        outflow[s] += v
        inflow[t] += v
    for n in list(all_nodes):
        kind = special.get(n, (None, None, "node"))[2]
        if kind != "node":
            continue
        gap = inflow[n] - outflow[n]
        if gap > 0:
            agg[(n, "SAVING")] = {"net saving / debt repayment": gap}
            outflow[n] += gap
            inflow["SAVING"] += gap
        elif gap < 0:
            agg[("CREDIT", n)] = {"net new borrowing / dis-saving": -gap}
            inflow[n] += -gap
            outflow["CREDIT"] += -gap
    assume(
        "Small links and balance",
        [
            f"The model removes links of less than ${MIN_EDGE:.0f}M per year. It keeps the 3 "
            "largest outflows of each node.",
            f"The removed links total ${dropped/1e3:,.1f}B.",
            "After this, the model balances each ordinary node exactly for the year.",
            'Extra inflow goes to "Net saving & debt repayment". Missing inflow comes from '
            '"Bank credit & dis-saving".',
            "Thus, for each node that is not a source or a sink, dollars in are equal to dollars "
            "out. The random-dollar walk needs this balance.",
        ],
    )
    # dwell times
    bus_dep = z1["nfc_chk"] + z1["nfc_ts"] + z1["nnc_chk"] + z1["nnc_ts"]
    sl_dep = z1["sl_chk"] + z1["sl_ts"]
    hh_dep = load_dfa_deposits(hh["dpi"])
    ind_out_total = sum(outflow[n] for n in node_meta)
    dwell = {}
    for n in node_meta:
        dwell[n] = bus_dep / ind_out_total * 365
    for h, hid in enumerate(hh_ids):
        dwell[hid] = hh_dep[h] / outflow[hid] * 365
    dwell["SL"] = sl_dep / outflow["SL"] * 365
    dwell["FRB"] = FRB_DWELL_DAYS
    assume(
        "How long a dollar stays at a node",
        [
            "Mean holding time is equal to money held divided by annual outflow (the inverse of "
            "velocity).",
            f"Deposit balances are the mean of the four quarters of {YEAR}. Source: Federal "
            "Reserve Financial Accounts (Z.1).",
            f"Businesses: nonfinancial corporate and noncorporate businesses hold "
            f"${bus_dep/1e6:.1f}T in checkable, time and savings deposits.",
            "The model divides this amount in proportion to outflows. Thus, all industries have "
            f"the same holding time: {dwell[next(iter(node_meta))]:.0f} days.",
            f"Households: deposits of ${hh_dep.sum()/1e6:.1f}T come from the Fed Distributional "
            "Financial Accounts (DFA), by income group.",
            "The DFA income groups are: bottom 20%, 20-40%, 40-60%, 60-80%, 80-99% and top 1%.",
            "The model divides each DFA group among its smaller groups by their share of "
            "disposable income.",
            "DFA ranks by household income, not by equivalized income.",
            "Balances, bottom to top: "
            + ", ".join(f"${d/1e3:,.0f}B" for d in hh_dep)
            + ".",
            f"State and local governments: ${sl_dep/1e6:.2f}T.",
            f"Federal Reserve: {FRB_DWELL_DAYS:.0f} days.",
            "In the trace, the model takes each stay from an exponential distribution with that "
            "mean.",
            "Sources hold money for 0 days. Sinks are the end of the path.",
        ],
    )
    assume(
        "Numbers entered by hand",
        [
            "Some inputs are typed in build_flows.py. The model does not read them from a "
            "downloaded file.",
            "To test sensitivity, change these values in build_flows.py.",
            f"Federal Reserve {YEAR} income statement: securities income "
            f"${sec_income/1e3:.1f}B, interest on reserves ${fedfin['int_reserves']/1e3:.1f}B, "
            f"interest on reverse repos ${fedfin['int_repo']/1e3:.1f}B.",
            (
                "These Fed values are copied exactly from the audited Federal Reserve Banks "
                "financial statements."
                if fed_audited
                else "These Fed values come from the Federal Reserve Board press release."
            ),
            f"Federal Reserve holding time: {FRB_DWELL_DAYS:.0f} days.",
            f"Banks get {HH_INTEREST_DEPOSITORY:.0%} of household interest payments.",
            f"Medicare and Medicaid pay a maximum of {GOV_HEALTH_CAP:.0%} of consumer spending at "
            "health providers.",
            "Private insurance claims have a limit, so that all payers together pay a maximum of "
            "95%.",
            "Household spending tilts by income quintile. These are approximate BLS 2017 budget "
            f"shares. The model uses them without change for {YEAR}.",
            f"Link threshold: ${MIN_EDGE:.0f}M per year.",
            "Two concordances: fixed-asset industries to IO detail industries (by NAICS prefix), "
            "and fixed-asset types to IO commodities (by NAICS code).",
            "Proxies for items that BEA does not divide by household group:",
            "Pension benefits use the shares of government benefits.",
            "Owner-occupied housing investment and employee pension contributions use wage shares.",
            "Household interest payments use consumer-spending shares.",
            "Fed reverse-repo interest uses interest-income shares.",
            (
                f"The {YEAR} projection uses no hand-entered scale factors. All factors come from "
                "BEA data."
                if YEAR > SUMMARY_LAST
                else None
            ),
        ],
    )
    assume(
        "Items that the model does not show",
        [
            "Purchases and sales of existing financial assets (stocks, bonds, property). These are "
            "swaps between savers. They show only in the net-saving and bank-credit nodes.",
            "Cross-border dividends and interest, except interest on federal debt.",
            "Subsidies. Taxes on production include them as a net value.",
            "Inventory decreases.",
            "Nonprofit institutions as separate nodes. BEA includes them in households and "
            "industries. This model does the same.",
            "Household type (children, age and other types).",
            "Timing in the year. Each flow is an annual mean.",
        ],
    )

    def node_url(n, codes):
        if n in NODE_URLS:
            return NODE_URLS[n]
        if n in hh_ids:
            return NODE_URLS["HH"]
        naics = [hier.get(c, {}).get("naics") for c in codes]
        naics = [x for x in naics if x]
        if not naics:
            return NODE_URLS["GOV_IND"]
        if (
            len(naics) > 1
        ):  # grouped node: longest common NAICS prefix (at least 2 digits)
            p = naics[0]
            while len(p) > 2 and not all(x.startswith(p) for x in naics):
                p = p[:-1]
            naics = [p]
        return f"https://www.census.gov/naics/?input={naics[0]}&year=2022&details={naics[0]}"

    nodes = []
    for n in sorted(all_nodes):
        if n in special:
            name, sector, kind = special[n]
            meta = {"codes": []}
        else:
            meta = node_meta[n]
            name, sector, kind = meta["name"], meta["sector"], "node"
        nodes.append(
            {
                "id": n,
                "name": name,
                "sector": sector,
                "kind": kind,
                "url": node_url(n, meta["codes"]),
                "codes": meta["codes"],
                "output": round(meta.get("output", 0) / 1e3, 3),
                "inflow": round(inflow[n] / 1e3, 3),
                "outflow": round(outflow[n] / 1e3, 3),
                "dwell": round(dwell.get(n, 0.0), 2),
            }
        )
    links = []
    for (s, t), types in agg.items():
        v = sum(types.values())
        links.append(
            {
                "s": s,
                "t": t,
                "v": round(v / 1e3, 4),
                "types": {
                    k_: round(x / 1e3, 4)
                    for k_, x in sorted(types.items(), key=lambda kv: -kv[1])
                },
            }
        )

    sources = [
        {
            "name": "BEA Input-Output Accounts: Use & Make tables, before redefinitions, 2017 detail",
            "url": "https://apps.bea.gov/industry/iTables%20Static%20Files/AllTablesIO.zip",
        },
        {
            "name": "BEA Input-Output Accounts: summary Use & Make tables 1997-2023 (same zip)",
            "url": "https://apps.bea.gov/industry/iTables%20Static%20Files/AllTablesIO.zip",
        },
        {
            "name": "BEA NIPA flat files (annual) - tables 1.12, 2.1, 3.2, 3.3, 3.12, 4.1, 7.20, 7.23",
            "url": "https://apps.bea.gov/national/Release/TXT/NipaDataA.txt",
        },
        {
            "name": "BEA NIPA series register (maps series codes to table lines)",
            "url": "https://apps.bea.gov/national/Release/TXT/SeriesRegister.txt",
        },
        {
            "name": "BEA Fixed Assets: investment in private nonresidential fixed assets by industry and asset type",
            "url": "https://apps.bea.gov/national/FA2004/Details/xls/detailnonres_inv1.xlsx",
        },
        {
            "name": "BEA Fixed Assets: private residential fixed assets by tenure",
            "url": "https://apps.bea.gov/national/FA2004/Details/xls/detailresidential.xlsx",
        },
        {
            "name": "BEA Distribution of Personal Income (summary file)",
            "url": "https://apps.bea.gov/data/special-topics/distribution-of-personal-income/national/summary-file.csv",
        },
        {
            "name": "BEA/BLS joint distribution of DPI and PCE",
            "url": "https://www.bea.gov/sites/default/files/2026-05/joint_dist_summary.xlsx",
        },
        {
            "name": "Federal Reserve Financial Accounts of the United States (Z.1), CSV tables",
            "url": "https://www.federalreserve.gov/releases/z1/current/z1_csv_files.zip",
        },
        {
            "name": "Federal Reserve Distributional Financial Accounts (deposits by income group)",
            "url": "https://www.federalreserve.gov/releases/z1/dataviz/download/zips/dfa.zip",
        },
    ]
    if "tsy" in fedfin:
        sources.append(
            {
                "name": f"Federal Reserve Banks: audited combined financial statements (covers {YEAR})",
                "url": f"https://www.federalreserve.gov/aboutthefed/files/combinedfinstmt{max(YEAR, 2024)}.pdf",
            }
        )
    else:
        sources.append(
            {
                "name": "Federal Reserve Board: Reserve Bank income & expenses (annual press releases)",
                "url": "https://www.federalreserve.gov/aboutthefed/audited-annual-financial-statements.htm",
            }
        )
    if YEAR > SUMMARY_LAST:
        sources += [
            {
                "name": "BEA GDP by Industry: gross output by industry (TGO105)",
                "url": "https://apps.bea.gov/industry/Release/XLS/GDPxInd/GrossOutput.xlsx",
            },
            {
                "name": "BEA GDP by Industry: value added and its components by industry (TVA105, TVA112)",
                "url": "https://apps.bea.gov/industry/Release/XLS/GDPxInd/ValueAdded.xlsx",
            },
        ]
    data = {
        "meta": {
            "year": YEAR,
            "nowcast": YEAR > SUMMARY_LAST,
            "units": "$ billions per year",
            "industryNodes": len(node_meta),
            "nodeCount": len(nodes),
            "linkCount": len(links),
        },
        "nodes": nodes,
        "links": links,
        "assumptions": ASSUMPTIONS,
        "sources": sources,
        "nipa": {k_: round(v / 1e3, 1) for k_, v in nipa.items()},
    }
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    out = OUT_DIR / f"flows_{YEAR}.js"
    out.write_text(
        "(window.SFC_DATA_BY_YEAR = window.SFC_DATA_BY_YEAR || {})["
        + str(YEAR)
        + "] = "
        + json.dumps(data, separators=(",", ":"))
        + ";\n",
        encoding="utf-8",
    )
    years = sorted(int(p.stem.split("_")[1]) for p in OUT_DIR.glob("flows_*.js"))
    (OUT_DIR / "years.js").write_text(
        f"window.SFC_YEARS = {json.dumps(years)};\n", encoding="utf-8"
    )
    tot_src = {n: outflow[n] / 1e3 for n in ("FED_SRC", "ROW_SRC", "CREDIT")}
    tot_snk = {n: inflow[n] / 1e3 for n in ("FED_SINK", "ROW_SINK", "SAVING")}
    print(
        f"nodes={len(nodes)} links={len(links)} sources($B)={tot_src} sinks($B)={tot_snk}"
    )
    print(f"wrote {out}")


if __name__ == "__main__":
    main()
