"""Build a US money-flow network (2017) from BEA data and write web/data/flows.js.

All dollar values are in $ millions (BEA convention) until written out.
Every modelling choice that is not straight from the data is recorded in ASSUMPTIONS
and shipped to the browser so it can be audited.
"""

import argparse
import csv
import json
from collections import defaultdict
from pathlib import Path

import numpy as np
import openpyxl
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
RAW = ROOT / "data" / "raw"
OUT = ROOT / "web" / "data" / "flows.js"
YEAR = 2023  # overridden by --year
IO_DETAIL_YEAR = 2017  # only year BEA publishes the 402-industry detail tables
SOURCE_YEARS = {}

# ---- Tunable assumptions (all $ millions unless noted) -------------------------------
FRB_DWELL_DAYS = 7.0  # Fed remits earnings to Treasury weekly
# Federal Reserve Banks' income statement, from the Board's annual income & expense press releases.
# 2023 reserve/repo split derived from the reported 2021->2022 and 2022->2023 changes.
FED_FINANCIALS = {
    2017: {"securities_income": 113.6e3, "int_reserves": 25.9e3, "int_repo": 4.0e3},
    2023: {"securities_income": 163.8e3, "int_reserves": 176.6e3, "int_repo": 104.5e3},
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
    "pct00to20": [0, 1], "pct20to40": [2, 3], "pct40to60": [4, 5],
    "pct60to80": [6, 7], "pct80to99": [8, 9, 10], "pct99to100": [11],
}
Z1_SERIES = {
    "hh_chk": "FL153020005", "hh_ts": "FL153030005", "nfc_chk": "FL103020005",
    "nfc_ts": "FL103030003", "nnc_chk": "FL113020005", "nnc_ts": "FL113030003",
    "sl_chk": "FL213020005", "sl_ts": "FL213030000", "fed_tsy": "FL713061163",
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


def assume(title, text):
    ASSUMPTIONS.append({"title": title, "text": text})


def code_str(v):
    if isinstance(v, float) and v.is_integer():
        v = int(v)
    return str(v).strip()


# ---- Loaders ---------------------------------------------------------------------------
def load_nipa():
    vals = {}
    wanted = set(NIPA_SERIES.values())
    with open(RAW / "NipaDataA.txt", encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            if row["%SeriesCode"] in wanted and row["Period"] == str(YEAR):
                vals[row["%SeriesCode"]] = float(row["Value"].replace(",", ""))
    missing = wanted - set(vals)
    if missing:
        raise SystemExit(f"Missing NIPA series: {missing}")
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
            info[code_str(c3)] = {
                "sector": sector,
                "summary": summary,
                "summary_name": summary_name,
                "name": str(c4).strip(),
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


def load_summary(kind):
    """BEA summary Use or Make table for YEAR as {(row code, col code): $M}."""
    df = pd.read_excel(
        RAW / "io" / f"IO{kind}_Before_Redefinitions_PRO_1997-2023_Summary.xlsx",
        sheet_name=str(YEAR),
        header=None,
    )
    cols = [code_str(c) if pd.notna(c) else None for c in df.iloc[5]]
    rows = [code_str(c) if pd.notna(c) else None for c in df.iloc[:, 0]]
    vals = df.apply(pd.to_numeric, errors="coerce").fillna(0.0).to_numpy()
    return {
        (r, c): vals[i, j]
        for i, r in enumerate(rows) if i >= 7 and r
        for j, c in enumerate(cols) if j >= 2 and c
    }


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
    d = d[(d.Year == dpi_year) & (d.Ranking == "Equivalized Disposable Personal Income")]
    d["Value"] = pd.to_numeric(d["Value"], errors="coerce")

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

    scale_note = ""
    if YEAR != IO_DETAIL_YEAR:
        su, sm = load_summary("Use"), load_summary("Make")
        summ = lambda c: hier.get(c, {}).get("summary", c)  # noqa: E731
        va_codes = ["V00100", "V00200", "V00300"]
        Z, l1, t1 = scale_blocks(Z, coms, inds, summ, summ, su)
        F, l2, t2 = scale_blocks(F, coms, fd, summ, lambda c: c[:4], su)
        VAm, l3, t3 = scale_blocks(
            np.vstack([VA[c] for c in va_codes]), va_codes, inds, lambda c: c[:4], summ, su
        )
        make, l4, t4 = scale_blocks(make, inds, coms, summ, summ, sm)
        VA = {c: VAm[k] for k, c in enumerate(va_codes)}
        VA["T008"] = Z.sum(0) + VAm.sum(0)
        lost = (l1 + l2 + l3 + l4) / (t1 + t2 + t3 + t4)
        scale_note = (
            f" BEA only publishes the 402-industry detail for {IO_DETAIL_YEAR}; for {YEAR} it publishes "
            "~71-industry summary Use and Make tables. Every block of the detail tables that falls in one "
            f"summary cell (summary commodity x summary industry/final-use column) is rescaled so it sums "
            f"exactly to BEA's {YEAR} summary value, keeping the {IO_DETAIL_YEAR} split inside the block. "
            f"So industry totals, value added, final demand and imports are {YEAR} values; only the "
            f"within-summary detail mix is from {IO_DETAIL_YEAR}. Summary cells with no {IO_DETAIL_YEAR} "
            f"detail counterpart are dropped ({lost:.2%} of {YEAR} table value)."
        )
    SOURCE_YEARS["input-output structure (detail)"] = IO_DETAIL_YEAR

    assume(
        "Base year and core data",
        f"Everything is a snapshot of annual flows in {YEAR}. Inter-industry flows come from the BEA "
        "detailed (402-industry) Use and Make tables, before redefinitions, producer prices."
        + scale_note
        + " Government, tax, transfer and income totals come from NIPA tables 1.12, 2.1, 3.2, 3.3, "
        "3.12, 4.1; pension cash flows from NIPA 7.20 (all plans) and 7.23 (federal plans). Investment "
        "by industry and asset type, and residential investment by tenure, come from BEA Fixed Assets "
        "detailed estimates. Household splits come from BEA Distribution of Personal Income "
        "(equivalized DPI ranking) and the BEA/BLS joint DPI-PCE distribution. Deposit balances come "
        "from the Fed's Financial Accounts (Z.1) and Distributional Financial Accounts. Units shown are "
        "$ billions per year. Data years used: "
        + ", ".join(f"{k} {v}" for k, v in {**SOURCE_YEARS, "everything else": YEAR}.items())
        + ". Different BEA tables are on slightly different vintages, so totals can differ by a few percent.",
    )

    # Drop imputed / non-monetary items
    drop = {"531HSO", "S00401", "S00402", "S00900"}
    com_idx = {c: i for i, c in enumerate(coms)}
    fdi = {c: j for j, c in enumerate(fd)}
    ooh = F[com_idx["531HSO"], fdi["F01000"]]
    assume(
        "Imputations removed",
        f'Owner-occupied housing (531HSO: imputed rent homeowners "pay themselves", ${ooh/1e3:,.0f}B of PCE) '
        "is removed, "
        "along with scrap, used goods and the rest-of-world adjustment rows. Rental income of persons "
        "(mostly that imputation) is not paid out; real-estate operating surplus is retained instead. "
        "Other imputations embedded in BEA data (e.g. imputed financial services, employer-paid "
        "benefits in kind) are NOT removed.",
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
        "A purchase of commodity c is paid to domestic industries in proportion to their production of "
        "c in the BEA Make table (market shares), after removing the imported share.",
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
        "For each commodity the import share = imports / domestic (non-export) use. Every domestic buyer "
        "of that commodity (industries, households, governments, investors) sends that share of its "
        "payment to the Rest of World (RoW). Exports are assumed to be 100% domestically produced. "
        "Noncomparable imports are 100% imported.",
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
        "Medicare, Medicaid & private health insurance",
        f'NIPA records Medicare (${nipa["medicare"]/1e3:.0f}B) and Medicaid (${nipa["medicaid"]/1e3:.0f}B) '
        "as transfers to persons, but the money actually goes to providers. Here the Federal government "
        "(Medicare) and State & local (Medicaid) pay ambulatory care, hospitals and nursing facilities "
        f"(NAICS 621-623) directly, covering {g:.0%} of PCE on those services. Employer health-insurance "
        f"contributions go to insurers, who pay providers claims covering a further {gi:.0%}. Households "
        "pay the remainder out of pocket. The government amounts are removed from household benefits.",
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
        "Total PCE per household group follows the BEA/BLS joint DPI-PCE distribution (2017). The mix "
        "of what each group buys starts from hand-entered budget-share tilts by income quintile "
        "(approximating BLS Consumer Expenditure Survey 2017; e.g. bottom quintile spends ~1.6x the "
        "average share on rent, top quintile ~1.35x on travel) and is then RAS-balanced so commodity "
        "totals match BEA PCE exactly. Commodity->category mapping is by NAICS prefix. Top 10% groups "
        "all use the top-quintile tilt.",
    )
    assume(
        "Household groups",
        "Households are 12 groups ranked by equivalized disposable personal income: deciles 1-9, "
        "90-95%, 95-99%, top 1% (BEA Distribution of Personal Income). Income components (wages, "
        "dividends, taxes...) are only published by decile, so the top decile is split using the "
        "DPI split (this understates the top 1%'s share of capital income). BEA does not publish a joint "
        "split by household type (e.g. with/without children), so household type is not modelled.",
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
        "Federal consumption + gross investment (defense & nondefense) is paid by the Federal "
        'spending node; this includes buying the output of "Federal general government" industries, '
        "which then pay federal employees. Same for State & local. Govt purchases are at producer prices.",
    )
    assume(
        "Investment",
        "Each industry pays for its own capital goods - there is no investment pool. BEA Fixed Assets "
        f"detailed estimates for {YEAR} give private nonresidential investment by {len(fa_members)} "
        "fixed-asset industries x ~95 asset types (e.g. manufacturing structures, mining & oilfield "
        "machinery, light trucks, prepackaged software, R&D). Each asset type is bridged to the IO "
        "commodities it is bought as (hand-made concordance by NAICS code, e.g. 'mining & oilfield "
        "machinery' -> 333130, 'manufacturing structures' -> 233230, all R&D -> 541700); trade and "
        "transport margins, installation and brokers' fees are shared across all asset types. A RAS "
        "balance makes the asset x commodity bridge match the IO investment columns (F02E00, F02S00, "
        "F02N00) exactly and the fixed-asset type totals as closely as the concordance allows "
        "(remaining mismatch vs fixed-asset totals: "
        + ", ".join(f"{k.lower()} {v:.1%}" for k, v in bridge_gap.items())
        + "). So each industry buys its own mix of capital goods, scaled to the IO totals. Within a "
        "fixed-asset industry the amount is split among its IO detail industries by gross output. "
        "Residential investment is split by "
        f"tenure from BEA's residential detail: {owner_share:.0%} owner-occupied, paid by household "
        "groups in proportion to their wage shares (a proxy for who buys/renovates homes), and the rest "
        "(rental housing) paid by the tenant-occupied housing industry. Positive inventory change is paid "
        "by goods-producing and trade industries in proportion to output; draw-downs are ignored. "
        "Retained earnings simply stay with each industry: if an industry invests more than it keeps it "
        "draws on bank credit, if less it saves. Purchases an industry makes from itself (e.g. construction "
        "building its own structures) are internal and not shown."
        + (
            f" Industries without fixed-asset data: {', '.join(unmatched)}."
            if unmatched
            else ""
        ),
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
    tsy_share = z1["fed_tsy"] / (z1["fed_tsy"] + z1["fed_agency"])
    fed_tsy_int = fedfin["securities_income"] * tsy_share
    fed_mbs_int = fedfin["securities_income"] - fed_tsy_int
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
        f"Compensation is split using NIPA 2017 ratios: {w_frac:.1%} wages & salaries to households "
        f"(by BEA compensation decile shares), {si_frac:.1%} employer social-insurance contributions "
        f"straight to the Federal govt, employer pension contributions to the pension sink "
        f"(see Pensions), and employer health/insurance contributions "
        f"(${employer_insurance/1e3:.0f}B = NIPA employer pension+insurance contributions minus cash "
        "pension contributions) to insurers, all in proportion to each industry's compensation. Cash "
        "paid slightly exceeds BEA compensation because NIPA's negative imputed pension contributions "
        "are excluded; the difference is covered by the credit node. Taxes on production go "
        f"{tpi_fed_frac:.1%} Federal / rest State & local (NIPA ratio, applied uniformly); subsidies ignored.",
    )
    assume(
        "Paying out operating surplus",
        f"Private gross operating surplus (${gos_priv_tot/1e3:,.0f}B after removing owner-occupied "
        "housing) is split in the SAME proportions for every industry: corporate income taxes (federal "
        f'${nipa["fed_corp"]/1e3:.0f}B, S&L ${nipa["sl_corp"]/1e3:.0f}B), dividends '
        f'${nipa["pers_div"]/1e3:.0f}B (of which ${nipa["pen_div"]/1e3:.0f}B to pension funds), cash '
        f"interest ${ind_interest/1e3:.0f}B (personal interest income minus that paid by governments "
        f'and minus ${nipa["pen_imp_int"]/1e3:.0f}B of non-cash imputed interest on pension claims; '
        f"${pen_int_nonfed/1e3:.0f}B of it to pension funds), proprietors' income "
        f'${nipa["proprietors"]/1e3:.0f}B; the remainder '
        f"(${(gos_priv_tot-payout_total)/1e3:,.0f}B) is retained by the industry to fund its own investment. "
        "Each income type is distributed to households by its own BEA decile shares. In reality payout "
        "ratios differ a lot by industry - this is a major simplification. Cross-border dividends / "
        "interest (other than on federal debt) are not modelled.",
    )
    assume(
        "Government industries",
        "Operating surplus of government industries and enterprises (mostly depreciation) is returned to "
        "the owning government. Federal: general government, Postal Service, federal enterprises. "
        "State & local: education, hospitals, other services, enterprises.",
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
        "Pension funds are a store of money for the future, so (like the Federal govt and RoW) they are "
        "split into a SINK and a SOURCE. Cash flows are from NIPA Table 7.20 (all DB + DC plans), with "
        "federal employee plans (Table 7.23) consolidated into the Federal government. Into the sink: "
        f"employer contributions ${pen_emp_nonfed/1e3:.0f}B (by industry compensation), employee "
        f"contributions ${pen_hh_nonfed/1e3:.0f}B (by household wage shares), dividends "
        f'${nipa["pen_div"]/1e3:.0f}B and monetary interest ${pen_int_nonfed/1e3:.0f}B (from industries '
        "by operating-surplus share). Out of the source: benefits & withdrawals "
        f"${pen_ben_nonfed/1e3:.0f}B to households (distributed like government social benefits, a "
        f'proxy for where retirees sit) and ${nipa["pen_admin"]/1e3:.0f}B of administrative fees to the '
        '"Funds, trusts & other financial vehicles" industry. Federal: agencies\' contributions '
        f'(${nipa["fpen_emp"]/1e3:.0f}B) go to the Federal sink, federal pensions '
        f'(${nipa["fpen_benefits"]/1e3:.0f}B) come from the Federal source, and Treasury interest to '
        "its own funds is netted out. Pension funds' purchases and sales of existing securities are "
        "asset swaps between savers and are not modelled.",
    )
    assume(
        "Taxes & transfers by household group",
        'Personal income taxes (federal and S&L) use BEA "Taxes" decile shares; employee social '
        'insurance contributions use the BEA "Contributions" shares; social benefits (ex Medicare/'
        'Medicaid) use the "Government social benefits" shares; interest on govt debt uses household '
        "interest-income shares (ownership via mutual funds is collapsed onto households). "
        "Household interest payments follow PCE shares and go "
        f"{HH_INTEREST_DEPOSITORY:.0%} to banks / rest to non-bank lenders. Remittances abroad follow "
        "wage shares.",
    )

    # Federal government, Fed, RoW
    add("FED_SRC", "SL", "grants-in-aid to states & localities", nipa["grants"])
    add("FED_SRC", "FRB", "interest on Treasuries held by the Fed", fed_tsy_int)
    add("I:" + bank_code, "FRB", "mortgage-backed securities interest passed to the Fed", fed_mbs_int)
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
        fedfin["int_reserves"] + fedfin["int_repo"] + nipa["fed_frb_remit"]
        - fedfin["securities_income"]
    )
    add("FED_SRC", "FRB", "Fed losses funded by new reserves (deferred asset)", fed_loss)
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
        "Money creation and destruction (MMT view)",
        "The Federal government is split into a SOURCE node (spending = crediting bank reserves = new "
        "money) and a SINK node (taxes, fees, Fed remittances = money deleted). State & local "
        "governments are currency USERS: an ordinary node that must tax/borrow before spending. The "
        "Rest of World is split into a SOURCE (export receipts bring dollars in) and a SINK (import "
        'payments send dollars out). Bank lending is the "horizontal" source of money: every node\'s '
        'gap between outflows and inflows is filled by "Bank credit & dis-saving" (source) or sent to '
        '"Net saving & debt repayment" (sink). Pension funds are also split into a source and a sink '
        "(see Pensions). The Federal Reserve is consolidated with the Treasury on the money-creation "
        f"side: in {YEAR} it earned ${fedfin['securities_income']/1e3:.0f}B on its securities (split "
        f"{tsy_share:.0%} Treasuries / {1-tsy_share:.0%} mortgage-backed by its Z.1 holdings; the MBS "
        "part arrives from the banking node, which collects mortgage interest), paid "
        f"${fedfin['int_reserves']/1e3:.0f}B interest on bank reserves and ${fedfin['int_repo']/1e3:.0f}B "
        "on reverse repos (passed to households via money market funds by interest-income shares) and "
        f'remitted ${nipa["fed_frb_remit"]/1e3:.1f}B. '
        + (
            f"Its ${fed_loss/1e3:.0f}B shortfall (booked as a 'deferred asset') is paid with newly "
            "created reserves, so it is shown as coming from the Federal money-creation node. "
            if fed_loss > 0
            else ""
        )
        + "Federal interest: "
        f"${fed_int_to_frb/1e3:.0f}B to the Federal Reserve, "
        f'${nipa["fed_int_row"]/1e3:.0f}B abroad, ${fed_int_persons/1e3:.0f}B to households.',
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
        f"The {len(ind_keep)} BEA detail industries are reduced to {len(node_meta)} nodes: the {k} "
        "largest by gross output are kept individually; the rest are merged within their BEA summary "
        'industry ("Other ..."). Rerun build_flows.py --industry-nodes N to change this (max ~400). '
        "Purchases between industries merged into the same node become internal and are not shown.",
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
        "Edge pruning & balancing",
        f"Edges under ${MIN_EDGE:.0f}M/yr are dropped unless they are among a node's 3 largest "
        f"outflows (${dropped/1e3:,.1f}B dropped in total). After pruning, every ordinary node is made "
        'exactly stock-flow consistent for the year: surplus inflow goes to "Net saving & debt '
        'repayment", deficits are drawn from "Bank credit & dis-saving". So for every non-source/sink '
        "node, dollars in = dollars out, which is what makes the random-dollar walk well defined.",
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
        "How long a dollar sits at a node",
        "Mean holding time = money held / annual outflow (inverse velocity), using average deposit "
        f"balances over the four quarters of {YEAR} from the Fed's Financial Accounts (Z.1). Businesses: "
        f"${bus_dep/1e6:.1f}T of checkable, time & savings deposits held by nonfinancial corporate and "
        "noncorporate business, spread in proportion to outflows -> "
        f"{dwell[next(iter(node_meta))]:.0f} days for every industry. Households: "
        f"${hh_dep.sum()/1e6:.1f}T of deposits from the Fed's Distributional Financial Accounts by "
        "income group (bottom 20%, 20-40, 40-60, 60-80, 80-99, top 1%); each band is split among our "
        "finer groups by their share of disposable income (DFA ranks by household income, not "
        f"equivalized income). Resulting balances bottom->top: "
        f'{", ".join(f"${d/1e3:,.0f}B" for d in hh_dep)}. State & local: ${sl_dep/1e6:.2f}T. Fed '
        f"{FRB_DWELL_DAYS:.0f} days. In the trace each stay is drawn from an exponential distribution "
        'with that mean (memoryless "pool" of money). Sources hold for 0 days; sinks are terminal.',
    )
    assume(
        "Numbers entered by hand (not downloaded)",
        "These inputs are typed into build_flows.py rather than read from a downloaded file; edit them "
        "there to test sensitivity. "
        f"(1) Federal Reserve {YEAR} income statement: securities income "
        f"${fedfin['securities_income']/1e3:.1f}B, interest on reserves ${fedfin['int_reserves']/1e3:.1f}B, "
        f"interest on reverse repos ${fedfin['int_repo']/1e3:.1f}B (Fed Board income & expense press "
        f"releases; the reserve/repo split is derived from reported year-on-year changes), and a "
        f"{FRB_DWELL_DAYS:.0f}-day Fed holding time. "
        f"(2) {HH_INTEREST_DEPOSITORY:.0%} of household interest payments go to banks. "
        f"(3) Medicare+Medicaid capped at {GOV_HEALTH_CAP:.0%} of PCE on health providers; private "
        "insurance claims capped so all payers together cover at most 95%. "
        "(4) Household spending tilts by income quintile (approximate BLS CE 2017 budget shares, "
        f"applied unchanged to {YEAR}). "
        f"(5) Edge-pruning threshold ${MIN_EDGE:.0f}M. "
        "(6) Two concordances: fixed-asset industries -> IO detail industries (by NAICS prefix) and "
        "fixed-asset types -> IO commodities (by NAICS code). "
        "(7) Proxies for who in the household distribution receives/pays things BEA does not split: "
        "pension benefits (like government benefits), owner-occupied housing investment and employee "
        "pension contributions (like wages), household interest payments (like consumer spending), "
        "Fed reverse-repo interest (like interest income).",
    )
    assume(
        "Not modelled",
        "Purchases and sales of existing financial assets (stocks, bonds, property) - they are swaps "
        "between savers and appear only as the net saving / bank credit residuals. Cross-border "
        "dividends and interest other than on federal debt. Subsidies (netted inside taxes on "
        "production). Inventory draw-downs. Nonprofit institutions as separate actors (folded into "
        "households and industries as BEA does). Household type (children, age, etc.). Within-year "
        "timing: every flow is an annual average.",
    )

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
        {
            "name": "Federal Reserve Board: Reserve Bank income & expenses 2023 (press release)",
            "url": "https://www.federalreserve.gov/newsevents/pressreleases/other20240112a.htm",
        },
        {
            "name": "Federal Reserve Board: Reserve Bank income & expenses 2022 (press release)",
            "url": "https://www.federalreserve.gov/newsevents/pressreleases/other20230113a.htm",
        },
    ]
    data = {
        "meta": {
            "year": YEAR,
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
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(
        "window.SFC_DATA = " + json.dumps(data, separators=(",", ":")) + ";\n",
        encoding="utf-8",
    )
    tot_src = {n: outflow[n] / 1e3 for n in ("FED_SRC", "ROW_SRC", "CREDIT")}
    tot_snk = {n: inflow[n] / 1e3 for n in ("FED_SINK", "ROW_SINK", "SAVING")}
    print(
        f"nodes={len(nodes)} links={len(links)} sources($B)={tot_src} sinks($B)={tot_snk}"
    )
    print(f"wrote {OUT}")


if __name__ == "__main__":
    main()
