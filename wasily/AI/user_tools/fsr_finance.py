# -*- coding: utf-8 -*-
"""发改委可研报告财务评价计算器。

只做确定性计算，不做假设：所有输入由调用方给出，缺失项用可解释默认值，
输出可直接写入报告的表格（list[list]）与指标，避免模型自己算错。

覆盖：投资估算与资金筹措、借款还本付息、总成本费用、营业收入与税金、
利润与利润分配、项目/资本金现金流量、FNPV、FIRR、静态/动态投资回收期、
总投资收益率、资本金净利润率、利息备付率、偿债备付率、资产负债率、
盈亏平衡分析、单因素敏感性分析。
"""

import json

SPEC = {
    "name": "fsr_finance",
    "description": "发改委可研报告财务评价计算：投资估算、资金筹措、借款还本付息、总成本费用、营业收入与税金、利润与利润分配、项目/资本金现金流量表，输出 FNPV、FIRR、静态/动态投资回收期、总投资收益率、资本金净利润率、利息备付率、偿债备付率、资产负债率、盈亏平衡点与敏感性分析表（金额单位：万元）",
    "parameters": {
        "construction_investment": "建设投资（万元，含工程费用+其他费用+预备费）",
        "interest_during_construction": "建设期利息（万元，可选，缺省按贷款与利率估算）",
        "working_capital": "流动资金（万元）",
        "capital_ratio": "项目资本金占总投资比例（0-1，默认0.3）",
        "loan_rate": "贷款年利率（如0.045）",
        "loan_years": "借款偿还期（年，默认10）",
        "annual_revenue": "达产年营业收入（不含税，万元）",
        "material_cost": "达产年外购原材料费（万元）",
        "fuel_cost": "达产年外购燃料动力费（万元）",
        "wage_cost": "达产年工资及福利费（万元）",
        "other_cost": "达产年其他费用（万元）",
        "repair_rate": "修理费费率（按固定资产原值，默认0.02）",
        "depreciation_years": "折旧年限（默认15）",
        "amortization": "无形资产及其他资产原值（万元，默认0）",
        "amortization_years": "摊销年限（默认10）",
        "residual_rate": "固定资产残值率（默认0.05）",
        "vat_rate": "增值税税率（默认0.13）",
        "surtax_rate": "附加税综合税率（占增值税，默认0.12）",
        "income_tax_rate": "企业所得税率（默认0.25）",
        "construction_years": "建设期（年，默认1）",
        "operation_years": "运营期（年，默认15）",
        "ic": "财务基准收益率（默认0.08）",
        "ramp": "投产期达产率列表，如 [0.6,0.8,1.0]（默认[0.7,0.9,1.0]）",
    },
}

_D = {
    "construction_investment": 10000.0, "interest_during_construction": None,
    "working_capital": 800.0, "capital_ratio": 0.3, "loan_rate": 0.045,
    "loan_years": 10, "annual_revenue": 6000.0, "material_cost": 1800.0,
    "fuel_cost": 300.0, "wage_cost": 500.0, "other_cost": 400.0,
    "repair_rate": 0.02, "depreciation_years": 15, "amortization": 0.0,
    "amortization_years": 10, "residual_rate": 0.05, "vat_rate": 0.13,
    "surtax_rate": 0.12, "income_tax_rate": 0.25, "construction_years": 1,
    "operation_years": 15, "ic": 0.08, "ramp": [0.7, 0.9, 1.0],
}


def _num(kw, key):
    v = kw.get(key)
    if v in (None, ""):
        v = _D[key]
    try:
        return float(v)
    except (TypeError, ValueError):
        return float(_D[key])


def _npv(rate, flows):
    return sum(f / ((1 + rate) ** i) for i, f in enumerate(flows))


def _irr(flows):
    lo, hi = -0.9, 1.0
    f_lo, f_hi = _npv(lo, flows), _npv(hi, flows)
    if f_lo * f_hi > 0:
        return None
    for _ in range(200):
        mid = (lo + hi) / 2
        f_mid = _npv(mid, flows)
        if abs(f_mid) < 1e-6:
            return mid
        if f_lo * f_mid < 0:
            hi, f_hi = mid, f_mid
        else:
            lo, f_lo = mid, f_mid
    return (lo + hi) / 2


def _payback(flows):
    """静态投资回收期（年，含建设期；线性插值）"""
    cum = 0.0
    for i, f in enumerate(flows):
        prev = cum
        cum += f
        if cum >= 0 and f > 0:
            return i - 1 + abs(prev) / f if i > 0 else 0.0
    return None


def _dpayback(flows, ic):
    cum = 0.0
    disc = [f / ((1 + ic) ** i) for i, f in enumerate(flows)]
    for i, f in enumerate(disc):
        prev = cum
        cum += f
        if cum >= 0 and f > 0:
            return i - 1 + abs(prev) / f if i > 0 else 0.0
    return None


def _evaluate(p):
    """按参数计算完整财务模型，返回内部结果字典"""
    n = int(p["construction_years"]) + int(p["operation_years"])
    op_years = int(p["operation_years"])
    ramp = p["ramp"] or [1.0]
    total_inv = p["construction_investment"] + p["interest_during_construction"] + p["working_capital"]
    capital = total_inv * p["capital_ratio"]
    loan = total_inv - capital
    fixed_asset = p["construction_investment"] + p["interest_during_construction"]
    dep_base = fixed_asset * (1 - p["residual_rate"])
    dep_annual = dep_base / max(1, p["depreciation_years"])
    amo_annual = p["amortization"] / max(1, p["amortization_years"])
    fixed_cost_y = p["wage_cost"] + p["other_cost"] + fixed_asset * p["repair_rate"]

    rows = []
    bal = loan
    principal = loan / max(1, p["loan_years"])
    for y in range(1, op_years + 1):
        interest = bal * p["loan_rate"]
        repay = principal if y <= p["loan_years"] else 0.0
        if repay > bal:
            repay = bal
        r = ramp[y - 1] if y <= len(ramp) else 1.0
        rev = p["annual_revenue"] * r
        var_cost = (p["material_cost"] + p["fuel_cost"]) * r
        dep = dep_annual if y <= p["depreciation_years"] else 0.0
        amo = amo_annual if y <= p["amortization_years"] else 0.0
        total_cost = var_cost + fixed_cost_y + dep + amo + interest
        op_cost = total_cost - dep - amo - interest
        vat = max(0.0, rev * p["vat_rate"] - var_cost * p["vat_rate"])
        surtax = vat * p["surtax_rate"]
        profit = rev - total_cost - surtax
        tax = max(0.0, profit) * p["income_tax_rate"]
        net = profit - tax
        ebit = profit + interest
        ebitda = ebit + dep + amo
        rows.append({
            "year": y, "ramp": r, "revenue": rev, "var_cost": var_cost,
            "op_cost": op_cost, "total_cost": total_cost, "dep": dep, "amo": amo,
            "interest": interest, "repay": repay, "balance": bal,
            "vat": vat, "surtax": surtax, "profit": profit, "tax": tax,
            "net": net, "ebit": ebit, "ebitda": ebitda,
            "icr": (ebit / interest) if interest > 1e-9 else None,
            "dscr": ((ebitda - tax) / (repay + interest)) if (repay + interest) > 1e-9 else None,
        })
        bal = bal - repay

    # 项目投资现金流量（所得税前/后）
    flows_bt, flows_at = [-p["construction_investment"] - p["working_capital"]], \
        [-p["construction_investment"] - p["working_capital"]]
    for i, r in enumerate(rows):
        last = (i == len(rows) - 1)
        salvage = fixed_asset * p["residual_rate"] if last else 0.0
        wc_back = p["working_capital"] if last else 0.0
        ncf = r["revenue"] - r["op_cost"] - r["surtax"] + salvage + wc_back
        flows_bt.append(ncf)
        flows_at.append(ncf - r["tax"])

    # 资本金现金流量
    equity = [-capital]
    for i, r in enumerate(rows):
        last = (i == len(rows) - 1)
        salvage = fixed_asset * p["residual_rate"] if last else 0.0
        wc_back = p["working_capital"] if last else 0.0
        equity.append(r["net"] + r["dep"] + r["amo"] - r["repay"] + salvage + wc_back)

    ic = p["ic"]
    op = rows
    avg = lambda k: sum(x[k] for x in op) / len(op) if op else 0.0
    years_with_int = [x for x in op if x["interest"] > 1e-9]
    icr = (sum(x["icr"] for x in years_with_int) / len(years_with_int)) if years_with_int else None
    yrs_with_debt = [x for x in op if (x["repay"] + x["interest"]) > 1e-9]
    dscr = (sum(x["dscr"] for x in yrs_with_debt) / len(yrs_with_debt)) if yrs_with_debt else None

    bep = None
    if p["annual_revenue"] > 0:
        denom = p["annual_revenue"] - (p["material_cost"] + p["fuel_cost"]) - \
            (p["annual_revenue"] * p["vat_rate"] * p["surtax_rate"])
        if denom > 0:
            bep = fixed_cost_y / denom * 100

    liab_ratio = [x["balance"] / max(1.0, fixed_asset + p["working_capital"]) * 100 for x in op]

    return {
        "years": n, "op_years": op_years, "rows": rows,
        "total_inv": total_inv, "capital": capital, "loan": loan,
        "fixed_asset": fixed_asset, "dep_annual": dep_annual, "amo_annual": amo_annual,
        "flows_bt": flows_bt, "flows_at": flows_at, "equity": equity,
        "fnpv_bt": _npv(ic, flows_bt), "fnpv_at": _npv(ic, flows_at),
        "firr_bt": _irr(flows_bt), "firr_at": _irr(flows_at), "firr_eq": _irr(equity),
        "payback": _payback(flows_at), "dpayback": _dpayback(flows_at, ic),
        "roi": (avg("ebit") / total_inv) if total_inv else None,
        "roe": (avg("net") / capital) if capital else None,
        "avg_icr": icr, "avg_dscr": dscr,
        "bep": bep, "avg_liab_ratio": (sum(liab_ratio) / len(liab_ratio)) if liab_ratio else None,
        "avg_revenue": avg("revenue"), "avg_total_cost": avg("total_cost"),
        "avg_net": avg("net"), "avg_profit": avg("profit"),
    }


def _pct(v, digits=2):
    return ("%." + str(digits) + "f%%") % (v * 100) if v is not None else "—"


def run(**kwargs):
    p = {}
    for k, dv in _D.items():
        if k == "ramp":
            v = kwargs.get(k)
            if isinstance(v, str):
                try:
                    v = json.loads(v)
                except Exception:
                    v = None
            p[k] = v or dv
            continue
        if k == "interest_during_construction":
            v = kwargs.get(k)
            p[k] = None if v in (None, "") else float(v)
            continue
        p[k] = _num(kwargs, k)

    # 建设期利息缺省估算：建设投资贷款 × 利率 × 建设期/2
    if p["interest_during_construction"] is None:
        p["interest_during_construction"] = p["construction_investment"] * \
            (1 - p["capital_ratio"]) * p["loan_rate"] * p["construction_years"] / 2

    r = _evaluate(p)
    op = r["rows"]

    def year_table():
        head = [["年份", "营业收入", "经营成本", "折旧摊销", "利息", "利润总额", "所得税", "净利润"]]
        for x in op:
            head.append([str(x["year"]), "%.0f" % x["revenue"], "%.0f" % x["op_cost"],
                         "%.0f" % (x["dep"] + x["amo"]), "%.0f" % x["interest"],
                         "%.0f" % x["profit"], "%.0f" % x["tax"], "%.0f" % x["net"]])
        return head

    def cash_table(flows):
        rows2 = [["年份", "净现金流量", "累计净现金流量", "折现净现金流量"]]
        cum = 0.0
        for i, f in enumerate(flows):
            cum += f
            d = f / ((1 + p["ic"]) ** i)
            rows2.append([str(i), "%.0f" % f, "%.0f" % cum, "%.1f" % d])
        return rows2

    def repay_table():
        rows3 = [["年份", "期初借款余额", "当年还本", "当年付息", "利息备付率", "偿债备付率"]]
        for x in op:
            if x["balance"] <= 1e-9 and x["repay"] <= 1e-9:
                continue
            rows3.append([str(x["year"]), "%.0f" % x["balance"], "%.0f" % x["repay"],
                          "%.0f" % x["interest"],
                          "%.2f" % x["icr"] if x["icr"] else "—",
                          "%.2f" % x["dscr"] if x["dscr"] else "—"])
        return rows3

    # 单因素敏感性：投资/售价/经营成本 ±10%
    def sens():
        base = dict(p)
        out = [["变化因素", "变化率", "财务内部收益率(FIRR)", "财务净现值(FNPV,万元)"]]
        cases = []
        for name, key, delta in (("建设投资", "construction_investment", 0.10),
                                 ("营业收入", "annual_revenue", 0.10),
                                 ("经营成本", "cost", 0.10)):
            for sign in (-1, 1):
                cases.append((name, key, sign * delta))
        for name, key, d in cases:
            q = dict(base)
            if key == "cost":
                for ck in ("material_cost", "fuel_cost", "wage_cost", "other_cost"):
                    q[ck] = base[ck] * (1 + d)
            else:
                q[key] = base[key] * (1 + d)
            rr = _evaluate(q)
            out.append([name, "%+d%%" % round(d * 100),
                        _pct(rr["firr_at"]), "%.1f" % rr["fnpv_at"]])
        return out

    inv_table = [["序号", "费用名称", "金额（万元）", "占总投资比例"],
                 ["1", "建设投资", "%.0f" % p["construction_investment"],
                  _pct(p["construction_investment"] / r["total_inv"])],
                 ["1.1", "　其中：建设期利息", "%.0f" % p["interest_during_construction"],
                  _pct(p["interest_during_construction"] / r["total_inv"])],
                 ["2", "流动资金", "%.0f" % p["working_capital"],
                  _pct(p["working_capital"] / r["total_inv"])],
                 ["3", "项目总投资", "%.0f" % r["total_inv"], "100.00%"],
                 ["3.1", "　其中：项目资本金", "%.0f" % r["capital"],
                  _pct(p["capital_ratio"])],
                 ["3.2", "　其中：债务资金", "%.0f" % r["loan"], _pct(1 - p["capital_ratio"])]]

    cost_table = [["项目", "达产年金额（万元）", "占营业收入比例"],
                  ["外购原材料费", "%.0f" % p["material_cost"],
                   _pct(p["material_cost"] / p["annual_revenue"] if p["annual_revenue"] else 0)],
                  ["外购燃料及动力费", "%.0f" % p["fuel_cost"],
                   _pct(p["fuel_cost"] / p["annual_revenue"] if p["annual_revenue"] else 0)],
                  ["工资及福利费", "%.0f" % p["wage_cost"],
                   _pct(p["wage_cost"] / p["annual_revenue"] if p["annual_revenue"] else 0)],
                  ["修理费", "%.0f" % (r["fixed_asset"] * p["repair_rate"]), "—"],
                  ["其他费用", "%.0f" % p["other_cost"],
                   _pct(p["other_cost"] / p["annual_revenue"] if p["annual_revenue"] else 0)],
                  ["折旧费", "%.0f" % r["dep_annual"], "—"],
                  ["摊销费", "%.0f" % r["amo_annual"], "—"],
                  ["经营成本合计", "%.0f" % (op[-1]["op_cost"] if op else 0), "—"],
                  ["总成本费用", "%.0f" % (op[-1]["total_cost"] if op else 0), "—"]]

    ind_table = [["指标", "计算结果", "评价标准"],
                 ["项目投资财务内部收益率（所得税前）", _pct(r["firr_bt"]), "≥ 基准收益率"],
                 ["项目投资财务内部收益率（所得税后）", _pct(r["firr_at"]), "≥ 基准收益率"],
                 ["项目资本金财务内部收益率", _pct(r["firr_eq"]), "≥ 资本金基准收益率"],
                 ["财务净现值（所得税后，ic=%s）" % _pct(p["ic"], 1), "%.1f 万元" % r["fnpv_at"], "≥ 0"],
                 ["静态投资回收期（所得税后）", ("%.1f 年" % r["payback"]) if r["payback"] else "—", "≤ 计算期"],
                 ["动态投资回收期（所得税后）", ("%.1f 年" % r["dpayback"]) if r["dpayback"] else "—", "≤ 计算期"],
                 ["总投资收益率（ROI）", _pct(r["roi"]), "≥ 行业平均"],
                 ["项目资本金净利润率（ROE）", _pct(r["roe"]), "≥ 行业平均"],
                 ["利息备付率（ICR，平均）", ("%.2f" % r["avg_icr"]) if r["avg_icr"] else "—", "≥ 1.0（宜≥2）"],
                 ["偿债备付率（DSCR，平均）", ("%.2f" % r["avg_dscr"]) if r["avg_dscr"] else "—", "≥ 1.0（宜≥1.2）"],
                 ["资产负债率（运营期平均）", _pct((r["avg_liab_ratio"] or 0) / 100), "≤ 70%"],
                 ["盈亏平衡点（生产能力利用率）", ("%.1f%%" % r["bep"]) if r["bep"] else "—", "越低越好"]]

    result = {
        "assumptions": {k: v for k, v in p.items()},
        "indicators": {
            "total_investment": round(r["total_inv"], 1),
            "capital": round(r["capital"], 1),
            "loan": round(r["loan"], 1),
            "fnpv_before_tax": round(r["fnpv_bt"], 1),
            "fnpv_after_tax": round(r["fnpv_at"], 1),
            "firr_before_tax": r["firr_bt"], "firr_after_tax": r["firr_at"],
            "firr_equity": r["firr_eq"],
            "payback_static": r["payback"], "payback_dynamic": r["dpayback"],
            "roi": r["roi"], "roe": r["roe"], "icr": r["avg_icr"], "dscr": r["avg_dscr"],
            "liability_ratio": r["avg_liab_ratio"], "bep": r["bep"],
        },
        "tables": {
            "投资估算与资金筹措表": inv_table,
            "总成本费用估算表（达产年）": cost_table,
            "利润与利润分配表": year_table(),
            "借款还本付息表": repay_table(),
            "项目投资现金流量表（所得税后）": cash_table(r["flows_at"]),
            "资本金现金流量表": cash_table(r["equity"]),
            "财务评价指标汇总表": ind_table,
            "敏感性分析表": sens(),
        },
        "summary": (
            "项目总投资 %.0f 万元（资本金 %.0f 万元、债务资金 %.0f 万元）；"
            "所得税后 FIRR %s，FNPV %.1f 万元（ic=%s），静态投资回收期 %s，"
            "总投资收益率 %s，资本金净利润率 %s。"
            % (r["total_inv"], r["capital"], r["loan"], _pct(r["firr_at"]),
               r["fnpv_at"], _pct(p["ic"], 1),
               ("%.1f 年" % r["payback"]) if r["payback"] else "无法回收",
               _pct(r["roi"]), _pct(r["roe"]))
        ),
    }
    return json.dumps(result, ensure_ascii=False, indent=1)
