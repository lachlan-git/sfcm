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
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
RAW = ROOT / "data" / "raw"
OUT = ROOT / "web" / "data" / "flows.js"
YEAR = 2017

# ---- Tunable assumptions (all $ millions unless noted) -------------------------------
HH_DEPOSITS = (
    10.5e6  # household transaction + savings deposits, approx Fed Z.1 B.101, 2017
)
BUS_DEPOSITS = 2.6e6  # nonfinancial business deposits + currency, approx Fed Z.1, 2017
SL_DEPOSITS = 0.4e6  # state & local govt deposits + currency, approx Fed Z.1, 2017
CAP_DWELL_DAYS = (
    90.0  # retained earnings wait this long on average before being invested
)
FRB_DWELL_DAYS = 7.0  # Fed remits earnings to Treasury weekly
IOER = 25.9e3  # Fed interest paid on reserve balances 2017 (approx, Fed annual report)
PENSION_SPLIT = 0.5  # share of employer pension+insurance contributions to pension funds (rest to insurers)
HH_INTEREST_DEPOSITORY = (
    0.7  # share of household interest payments going to banks (rest non-bank lenders)
)
GOV_HEALTH_CAP = (
    0.85  # Medicare+Medicaid can fund at most this share of PCE on health providers
)
MIN_EDGE = 25.0  # drop edges below $25M/yr (except each node's top 3 outflows)

# Household deposit shares (sum to 1) – roughly follows SCF 2016/2019 transaction-account
# concentration by income; used only for dwell-time (velocity) estimates.
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
HH_DEPOSIT_SHARE = [
    0.008,
    0.012,
    0.018,
    0.025,
    0.035,
    0.045,
    0.06,
    0.08,
    0.12,
    0.12,
    0.20,
    0.277,
]
HH_QUINTILE = [0, 0, 1, 1, 2, 2, 3, 3, 4, 4, 4, 4]

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
}

ASSUMPTIONS = []


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
        sheet_name=str(YEAR),
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
        sheet_name=str(YEAR),
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


def load_household_shares():
    d = pd.read_csv(RAW / "dpi_summary.csv")
    d = d[(d.Year == YEAR) & (d.Ranking == "Equivalized Disposable Personal Income")]
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
    j = pd.read_excel(RAW / "joint_dist.xlsx", sheet_name="shares of NIPA totals")
    j = j[
        (j.Year == YEAR)
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
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--industry-nodes",
        type=int,
        default=150,
        help="target number of industry nodes (max ~400 = full BEA detail)",
    )
    args = ap.parse_args()

    nipa = load_nipa()
    hier = load_hierarchy()
    use = load_use()
    inds, coms, fd = use["industries"], use["commodities"], use["fd"]
    Z, F, VA = use["Z"], use["F"], use["VA"]
    make = load_make(inds, coms)
    hh = load_household_shares()

    assume(
        "Base year and core data",
        f"Everything is a snapshot of annual flows in {YEAR}. Inter-industry flows come from the BEA "
        f"detailed (402-industry) Use and Make tables, before redefinitions, producer prices. Government, "
        "tax, transfer and income totals come from NIPA tables 1.12, 2.1, 3.2, 3.3, 3.12, 4.1. Household "
        "splits come from BEA Distribution of Personal Income (equivalized DPI ranking) and the BEA/BLS "
        "joint DPI-PCE distribution. Units shown are $ billions per year.",
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

    pension_code = find("Funds, trusts")
    insurer_code = find("Insurance carriers, except direct life")
    bank_code = find("Monetary authorities")
    nonbank_code = find("Nondepository credit")
    comp_io_total = float(sum(VA["V00100"][j] for j in ind_keep))
    pen_frac = nipa["emp_pension"] / nipa["comp"]
    employer_insurance = comp_io_total * pen_frac * (1 - PENSION_SPLIT)
    employer_pension = comp_io_total * pen_frac * PENSION_SPLIT

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
    inv_cols = [c for c in fd if c.startswith("F02")]
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
    inv_vec = sum(np.clip(F[:, fdi[c]], 0, None) for c in inv_cols) + np.clip(
        F[:, fdi["F03000"]], 0, None
    )
    buy("CAP", inv_vec, "investment purchases")
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
        "All private fixed investment (equipment, IP, structures, residential) and positive inventory "
        'change is paid from a single "Private investment pool" node funded by industries\' retained '
        "operating surplus; any shortfall is financed by new bank credit. Inventory draw-downs are ignored.",
    )

    # Value added
    comp, tpi, gos, output = VA["V00100"], VA["V00200"], VA["V00300"], VA["T008"]
    w_frac = nipa["wages"] / nipa["comp"]
    si_frac = nipa["emp_si"] / nipa["comp"]
    tpi_fed_frac = nipa["fed_tpi"] / (nipa["fed_tpi"] + nipa["sl_tpi"])

    private = [j for j in ind_keep if gov_level(inds[j]) is None]
    gos_priv = np.clip(gos, 0, None)
    gos_priv_tot = sum(gos_priv[j] for j in private)
    fed_int_to_frb = nipa["fed_frb_remit"] + IOER
    fed_int_res = nipa["fed_interest"] - nipa["fed_int_row"]
    fed_int_persons = fed_int_res - fed_int_to_frb
    ind_interest = nipa["pers_interest"] - fed_int_persons - nipa["sl_interest"]
    payouts = {
        "corporate income tax (federal)": ("FED_SINK", nipa["fed_corp"]),
        "corporate income tax (state & local)": ("SL", nipa["sl_corp"]),
        "dividends": ("div", nipa["pers_div"]),
        "interest paid to households": ("interest", ind_interest),
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
        add(
            src,
            "I:" + pension_code,
            "employer pension contributions",
            comp[j] * pen_frac * PENSION_SPLIT,
        )
        add(
            src,
            "I:" + insurer_code,
            "employer health/insurance contributions",
            comp[j] * pen_frac * (1 - PENSION_SPLIT),
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
            if dst in ("FED_SINK", "SL"):
                add(src, dst, typ, amt)
            else:
                for h in range(H):
                    add(src, hh_ids[h], typ, amt * hh[dst][h])
        add(
            src,
            "CAP",
            "retained earnings & depreciation",
            gos_priv[j] - payout_total * share,
        )
    assume(
        "Paying out value added",
        f"Compensation is split using NIPA 2017 ratios: {w_frac:.1%} wages & salaries to households "
        f"(by BEA compensation decile shares), {si_frac:.1%} employer social-insurance contributions "
        f"straight to the Federal govt, {pen_frac:.1%} employer pension & insurance contributions "
        f"({PENSION_SPLIT:.0%} to pension funds, rest to insurers). Taxes on production go "
        f"{tpi_fed_frac:.1%} Federal / rest State & local (NIPA ratio, applied uniformly); subsidies ignored.",
    )
    assume(
        "Paying out operating surplus",
        f"Private gross operating surplus (${gos_priv_tot/1e3:,.0f}B after removing owner-occupied "
        "housing) is split in the SAME proportions for every industry: corporate income taxes (federal "
        f'${nipa["fed_corp"]/1e3:.0f}B, S&L ${nipa["sl_corp"]/1e3:.0f}B), dividends '
        f'${nipa["pers_div"]/1e3:.0f}B, interest to households ${ind_interest/1e3:.0f}B (personal '
        "interest income minus that paid by governments), proprietors' income "
        f'${nipa["proprietors"]/1e3:.0f}B; the remainder '
        f"(${(gos_priv_tot-payout_total)/1e3:,.0f}B) is retained and sent to the Private investment pool. "
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
            "I:" + pension_code,
            hid,
            "pension benefits paid",
            employer_pension * hh["benefits"][h],
        )
    assume(
        "Pensions",
        'Employer pension contributions flow to "Funds, trusts and other financial vehicles", which are '
        "assumed to pay out the same amount as pension benefits that year (steady state), distributed "
        "like government social benefits (a proxy for where retirees sit in the income distribution). "
        "Pension benefits are not personal income in NIPA, but they are real money flows.",
    )
    assume(
        "Taxes & transfers by household group",
        'Personal income taxes (federal and S&L) use BEA "Taxes" decile shares; employee social '
        'insurance contributions use the BEA "Contributions" shares; social benefits (ex Medicare/'
        'Medicaid) use the "Government social benefits" shares; interest on govt debt uses household '
        "interest-income shares (ultimate ownership via funds/pensions is collapsed onto households). "
        "Household interest payments follow PCE shares and go "
        f"{HH_INTEREST_DEPOSITORY:.0%} to banks / rest to non-bank lenders. Remittances abroad follow "
        "wage shares.",
    )

    # Federal government, Fed, RoW
    add("FED_SRC", "SL", "grants-in-aid to states & localities", nipa["grants"])
    add("FED_SRC", "FRB", "interest on Treasuries held by the Fed", fed_int_to_frb)
    add("FRB", "FED_SINK", "Fed remittances to Treasury", nipa["fed_frb_remit"])
    add("FRB", "I:" + bank_code, "interest on reserve balances", IOER)
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
        '"Net saving & debt repayment" (sink). Federal interest: '
        f"${fed_int_to_frb/1e3:.0f}B to the Federal Reserve (= remittances + interest on reserves), "
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
        "CAP": ("Private investment pool (retained earnings)", "Investment", "node"),
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
    ind_out_total = sum(outflow[n] for n in node_meta)
    dwell = {}
    for n in node_meta:
        dwell[n] = BUS_DEPOSITS / ind_out_total * 365
    for h, hid in enumerate(hh_ids):
        dwell[hid] = HH_DEPOSITS * HH_DEPOSIT_SHARE[h] / outflow[hid] * 365
    dwell["SL"] = SL_DEPOSITS / outflow["SL"] * 365
    dwell["CAP"] = CAP_DWELL_DAYS
    dwell["FRB"] = FRB_DWELL_DAYS
    assume(
        "How long a dollar sits at a node",
        "Mean holding time = money held / annual outflow (inverse velocity). Businesses: ~$"
        f"{BUS_DEPOSITS/1e6:.1f}T of deposits & currency (approx. Fed Z.1) spread in proportion to "
        f"outflows -> {dwell[next(iter(node_meta))]:.0f} days for every industry. Households: ~$"
        f"{HH_DEPOSITS/1e6:.1f}T of deposits (approx. Z.1), allocated across groups by assumed shares "
        f'{", ".join(f"{s:.1%}" for s in HH_DEPOSIT_SHARE)} (bottom->top, roughly SCF concentration). '
        f"State & local: ~${SL_DEPOSITS/1e6:.1f}T. Investment pool {CAP_DWELL_DAYS:.0f} days, Fed "
        f"{FRB_DWELL_DAYS:.0f} days. In the trace each stay is drawn from an exponential distribution "
        'with that mean (memoryless "pool" of money). Sources hold for 0 days; sinks are terminal.',
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
            "name": "BEA NIPA flat files (annual)",
            "url": "https://apps.bea.gov/national/Release/TXT/NipaDataA.txt",
        },
        {
            "name": "BEA Distribution of Personal Income (summary file)",
            "url": "https://apps.bea.gov/data/special-topics/distribution-of-personal-income/national/summary-file.csv",
        },
        {
            "name": "BEA/BLS joint distribution of DPI and PCE",
            "url": "https://www.bea.gov/sites/default/files/2026-05/joint_dist_summary.xlsx",
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
