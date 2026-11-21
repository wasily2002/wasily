# -*- coding: utf-8 -*-
"""一键生成《发改委可行性研究报告》公文格式 docx。

为什么单独做成工具：7B 本地模型无法在一次工具调用里稳定产出上百个 blocks，
让它直接调 doc_write 往往会把 blocks 参数丢掉。这里把长模板放进代码，
模型只需要传项目基本信息和财务参数（十几项），文档质量与格式才可控。
"""

import importlib
import json
import os
import sys

SPEC = {
    "name": "fsr_report",
    "description": "一键生成发改委可行性研究报告（公文格式 Word）。内部自动完成财务测算（FIRR/FNPV/回收期/ROI/ROE/ICR/DSCR/BEP/敏感性）并排版 16 章正文与全部财务附表，模型只需传项目基本信息",
    "parameters": {
        "project_name": "项目名称，如 XX市智慧冷链物流产业园建设项目",
        "unit": "建设单位名称",
        "location": "建设地点",
        "output_path": "可选，输出 docx 路径；缺省写到 outputs/项目名称-可行性研究报告.docx",
        "main_recipient": "可选，主送机关，默认“XX市发展和改革委员会：”",
        "date": "可选，落款日期，默认今天",
        "construction_investment": "建设投资（万元），默认22100",
        "interest_during_construction": "建设期利息（万元），默认460",
        "working_capital": "流动资金（万元），默认1300",
        "capital_ratio": "资本金比例，默认0.3",
        "loan_rate": "贷款年利率，默认0.0435",
        "annual_revenue": "达产年营业收入（万元），默认12600",
        "material_cost": "外购原材料费（万元），默认1800",
        "fuel_cost": "外购燃料动力费（万元），默认1250",
        "wage_cost": "工资及福利费（万元），默认960",
        "other_cost": "其他费用（万元），默认620",
        "amortization": "无形资产原值（万元），默认600",
        "construction_years": "建设期（年），默认2",
        "operation_years": "运营期（年），默认15",
        "ic": "财务基准收益率，默认0.08",
        "scale": "可选，建设规模描述，默认“总建筑面积68000平方米”",
        "content": "可选，建设内容描述",
    },
}

_HERE = os.path.dirname(os.path.abspath(__file__))
_AI = os.path.dirname(_HERE)
for _p in (_AI, _HERE):
    if _p not in sys.path:
        sys.path.insert(0, _p)


def _load(name):
    try:
        return importlib.import_module(name)
    except Exception:
        spec = importlib.util.spec_from_file_location(name, os.path.join(_AI if name == "tools" else _HERE, name + ".py"))
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod


def run(project_name="", unit="XX市农业发展集团有限公司", location="XX市临港经济开发区",
        output_path="", main_recipient="XX市发展和改革委员会：", date="2026年11月14日",
        construction_investment=22100, interest_during_construction=460,
        working_capital=1300, capital_ratio=0.3, loan_rate=0.0435, loan_years=10,
        annual_revenue=12600, material_cost=1800, fuel_cost=1250, wage_cost=960,
        other_cost=620, amortization=600, construction_years=2, operation_years=15,
        ic=0.08, scale="总建筑面积68000平方米，其中冷库42000平方米、分拣加工车间12000平方米、"
                       "综合服务楼8000平方米、配套用房6000平方米",
        content=""):
    fsr = _load("fsr_finance")
    tools = _load("tools")

    project_name = (project_name or "XX市智慧冷链物流产业园建设项目").strip()
    short = project_name.replace("建设项目", "").replace("项目", "")

    d = json.loads(fsr.run(
        construction_investment=construction_investment,
        interest_during_construction=interest_during_construction,
        working_capital=working_capital, capital_ratio=capital_ratio,
        loan_rate=loan_rate, loan_years=loan_years, annual_revenue=annual_revenue,
        material_cost=material_cost, fuel_cost=fuel_cost, wage_cost=wage_cost,
        other_cost=other_cost, amortization=amortization,
        construction_years=construction_years, operation_years=operation_years,
        ic=ic, ramp=[0.6, 0.85, 1.0]))
    I, T = d["indicators"], d["tables"]
    P = lambda t: {"t": "p", "text": t}
    H1 = lambda t: {"t": "h1", "text": t}
    H2 = lambda t: {"t": "h2", "text": t}
    pct = lambda v, n=2: ("%." + str(n) + "f%%") % (v * 100)
    ti, cap, loan = I["total_investment"], I["capital"], I["loan"]
    content = content or ("主要建设冷库、分拣加工车间、综合服务楼及配套设施，并购置制冷、自动化分拣和"
                          "智能仓储管理设备")

    B = []
    a = B.append
    a({"t": "to", "text": main_recipient})
    a(P("根据国家和地方现代物流业发展规划及有关政策要求，%s组织编制了《%s可行性研究报告》。"
        "现将项目有关情况报告如下。" % (unit, project_name)))

    a(H1("一、项目概述"))
    a(H2("（一）项目概况"))
    a(P("项目名称为%s，建设单位为%s，建设地点位于%s，项目性质为新建。项目用地约120亩，%s。"
        "%s。项目建设期%d年，运营期%d年。项目总投资%.0f万元，其中建设投资%.0f万元、"
        "建设期利息%.0f万元、流动资金%.0f万元；资金来源为项目资本金%.0f万元（占%.0f%%），"
        "银行贷款%.0f万元（占%.0f%%），贷款年利率%.2f%%。"
        % (project_name, unit, location, scale, content, construction_years, operation_years,
           ti, construction_investment, interest_during_construction, working_capital,
           cap, capital_ratio * 100, loan, (1 - capital_ratio) * 100, loan_rate * 100)))
    a(H2("（二）编制依据"))
    a(P("1.《政府投资条例》；2.《投资项目可行性研究指南（2023年版）》；"
        "3.《“十四五”冷链物流发展规划》；4.XX市现代物流业发展规划；"
        "5.XX市国土空间总体规划；6.项目单位提供的基础资料及现场踏勘成果。"
        "【待核实：请按项目实际替换为准确的规划名称、发文机关与文号。】"))
    a(H2("（三）研究范围"))
    a(P("本报告研究项目建设背景与必要性、市场需求与建设规模、场址选择与建设条件、工程与技术方案、"
        "节能环保与劳动安全、组织机构与实施进度、投资估算与资金筹措、财务评价与经济分析、"
        "风险分析与对策，并据此提出项目是否可行的结论性意见。"))
    a(H2("（四）主要结论"))
    a(P("1.项目符合产业政策，是补齐区域设施短板的必要举措；2.建设规模依据需求测算确定，规模适度；"
        "3.场址条件良好，工程方案技术可行；4.项目总投资%.0f万元，所得税后财务内部收益率%s，"
        "财务净现值%.1f万元，静态投资回收期%.1f年，总投资收益率%s，资本金净利润率%s，财务上可行；"
        "5.项目社会效益显著，风险总体可控。结论：项目可行。"
        % (ti, pct(I["firr_after_tax"]), I["fnpv_after_tax"], I["payback_static"],
           pct(I["roi"]), pct(I["roe"]))))

    a(H1("二、项目建设背景与必要性"))
    a(H2("（一）政策背景"))
    a(P("国家层面先后出台《“十四五”冷链物流发展规划》《关于推动冷链物流高质量发展的意见》，"
        "支持农产品产地冷藏保鲜设施和县域商业体系建设。项目已列入XX市现代物流业发展规划重点项目，"
        "建设符合国家和地方产业政策导向。"))
    a(H2("（二）区域现状"))
    a(P("XX市是区域重要的农产品生产加工基地，现有仓储设施规模偏小、分布分散、设施老化，"
        "产地预冷、全程温控和集中分拨能力不足，流通损耗率高于全国平均水平。"
        "【待核实：农产品产量、流通量与现有设施容量需以统计年鉴和行业主管部门数据为准。】"))
    a(H2("（三）建设必要性"))
    a(P("一是补齐设施短板的迫切需要；二是降低流通损耗、促进农民增收的有效途径；"
        "三是保障食品安全和城市供应的重要支撑；四是推动产业化与物流业融合升级的现实选择；"
        "五是带动就业和增加地方税收。"))

    a(H1("三、需求分析与建设规模"))
    a(H2("（一）需求预测"))
    a(P("依据区域农产品产量、流通量和周转次数测算设施需求，并考虑加工与储备需求、留有一定弹性，"
        "结合服务半径和分期建设安排确定本期建设规模。【待核实：需求测算的具体数据需补充。】"))
    a(H2("（二）建设规模"))
    a(P("项目%s。%s，形成集产地预冷、集中仓储、分拣加工、城市配送和应急保供于一体的"
        "区域物流集配中心。" % (scale, content)))
    a(H2("（三）功能定位"))
    a(P("项目定位为区域农产品物流集配中心，重点承接预冷、仓储、加工、配送和应急保供五项功能，"
        "形成“一园多能、冷储结合、产销衔接”的运营模式。"))

    a(H1("四、建设条件与场址选择"))
    a(H2("（一）场址位置与用地"))
    a(P("场址位于%s，交通条件良好，地块为规划物流仓储用地，已完成征收和场地平整，具备开工条件。"
        % location))
    a(H2("（二）自然条件"))
    a(P("项目区气候、地质条件满足建设要求，抗震设防烈度按7度考虑。"
        "【待核实：地基承载力等参数以正式岩土工程勘察报告为准。】"))
    a(H2("（三）配套条件"))
    a(P("市政给水、排水、电力、通信、燃气等基础设施配套齐全，可满足项目建设和运营需要。"))
    a(H2("（四）场址比选"))
    a(P("综合比较交通衔接、土地条件、基础设施配套和产业协同等因素，推荐现选址作为项目建设场址。"))

    a(H1("五、建设方案"))
    a(H2("（一）总平面布置"))
    a(P("按功能分区布置，人流与货流分离，内部道路满足大型货车通行和消防扑救要求；"
        "合理确定容积率、建筑密度和绿地率。"))
    a(H2("（二）建筑与结构"))
    a(P("主体采用钢筋混凝土框架结构，大跨度仓储部分采用门式刚架，围护结构采用高效保温材料；"
        "建筑结构安全等级二级，设计使用年限50年，按7度抗震设防。"))
    a(H2("（三）给排水、电气与消防"))
    a(P("给水由市政管网供给，场内雨污分流，废水经预处理后接入市政污水管网；"
        "设双回路供电并配置备用电源；按规范设置室内外消火栓、自动灭火、火灾报警和防排烟系统。"))
    a(H2("（四）制冷与智能化"))
    a(P("制冷系统采用高效节能机组，配置变频控制、热回收和温湿度自动监测；"
        "建设仓储管理、运输管理、温控和能耗管理系统，实现全过程数字化管理。"))

    a(H1("六、技术方案、节能与环保"))
    a(H2("（一）技术方案与设备"))
    a(P("项目采用成熟可靠的工艺与设备，主要设备通过公开招标采购，技术方案处于区域先进水平。"
        "设备购置及安装费按估算列入投资。"))
    a(H2("（二）节能评价"))
    a(P("项目采取围护结构保温、变频控制、热回收、屋面光伏和智能能耗管理等措施，"
        "单位产出能耗低于行业平均水平，符合节能要求。"))
    a(H2("（三）环境保护"))
    a(P("施工期采取围挡、洒水、限时施工等措施控制扬尘和噪声；运营期废水经预处理后纳管，"
        "废气、固废分类收集处理，制冷剂选用低GWP工质并建立回收台账。项目不涉及环境敏感区，"
        "环境影响可控。"))

    a(H1("七、组织机构与实施进度"))
    a(H2("（一）组织机构与人力资源"))
    a(P("成立项目公司负责建设与运营，下设综合管理、财务、市场运营、仓储管理、工程技术和安全环保等部门，"
        "项目定员按运营需要配置，建立岗前培训和持证上岗制度。"))
    a(H2("（二）实施进度"))
    a(P("项目建设期%d年：前期手续与设计约7个月，土建施工与设备安装约14个月，"
        "调试验收约3个月，随后投入运营。具体进度见附件。" % construction_years))
    a(H2("（三）招标方案"))
    a(P("勘察设计、施工、监理和主要设备采购依法公开招标，招标组织形式为委托招标，"
        "招标公告按规定在指定媒介发布。"))

    a(H1("八、投资估算与资金筹措"))
    a(H2("（一）估算依据"))
    a(P("依据建设项目投资估算编审规程、现行定额与费用标准、当地市场价格和设备询价资料，"
        "参考区域内同类项目造价指标进行估算。"))
    a(H2("（二）投资估算"))
    a({"t": "table", "rows": [
        ["序号", "费用名称", "金额（万元）", "占总投资比例"],
        ["1", "建设投资", "%.0f" % construction_investment, pct(construction_investment / ti)],
        ["1.1", "　其中：建设期利息", "%.0f" % interest_during_construction,
         pct(interest_during_construction / ti)],
        ["2", "流动资金", "%.0f" % working_capital, pct(working_capital / ti)],
        ["3", "项目总投资", "%.0f" % ti, "100.00%"],
        ["3.1", "　其中：项目资本金", "%.0f" % cap, pct(capital_ratio)],
        ["3.2", "　其中：债务资金", "%.0f" % loan, pct(1 - capital_ratio)],
    ]})
    a(H2("（三）资金筹措"))
    a({"t": "table", "rows": [
        ["资金来源", "金额（万元）", "比例", "说明"],
        ["项目资本金", "%.0f" % cap, pct(capital_ratio), "企业自有资金，按进度投入"],
        ["银行贷款", "%.0f" % loan, pct(1 - capital_ratio), "年利率%.2f%%，期限%d年，等额本金"
         % (loan_rate * 100, loan_years)],
        ["合计", "%.0f" % ti, "100%", "—"],
    ]})
    a(P("项目资本金比例符合国家固定资产投资项目资本金制度要求，贷款已取得银行意向，资金落实。"))

    a(H1("九、财务评价"))
    a(H2("（一）评价依据与参数"))
    a(P("依据《建设项目经济评价方法与参数（第三版）》评价。计算期%d年（建设期%d年、运营期%d年），"
        "财务基准收益率%.0f%%；增值税税率9%%，附加税按增值税的12%%计取，企业所得税税率25%%；"
        "固定资产折旧采用直线法，残值率5%%，无形资产按10年摊销。"
        % (construction_years + operation_years, construction_years, operation_years, ic * 100)))
    a(H2("（二）营业收入与税金"))
    a(P("项目达产年营业收入%.0f万元，投产期按60%%、85%%、100%%达产。达产年应缴增值税及附加"
        "按测算列入成本费用。" % annual_revenue))
    a(H2("（三）总成本费用"))
    _tc = float(T["总成本费用估算表（达产年）"][-1][1])
    _oc = float(T["总成本费用估算表（达产年）"][-2][1])
    a(P("达产年总成本费用%.0f万元，其中经营成本%.0f万元。成本构成合理，处于同类项目正常水平。"
        % (_tc, _oc)))
    a({"t": "table", "rows": T["总成本费用估算表（达产年）"]})
    a(H2("（四）利润与利润分配"))
    a({"t": "table", "rows": T["利润与利润分配表"]})
    a(H2("（五）借款还本付息"))
    a({"t": "table", "rows": T["借款还本付息表"]})
    a(H2("（六）项目投资现金流量"))
    a({"t": "table", "rows": T["项目投资现金流量表（所得税后）"]})
    a(H2("（七）资本金现金流量"))
    a({"t": "table", "rows": T["资本金现金流量表"]})
    a(H2("（八）盈利能力分析"))
    a(P("所得税前财务内部收益率%s，所得税后财务内部收益率%s，均高于%.0f%%的基准收益率；"
        "所得税后财务净现值%.1f万元，大于零；静态投资回收期%.1f年，动态投资回收期%.1f年；"
        "总投资收益率%s，资本金净利润率%s。项目盈利能力较强。"
        % (pct(I["firr_before_tax"]), pct(I["firr_after_tax"]), ic * 100,
           I["fnpv_after_tax"], I["payback_static"], I["payback_dynamic"],
           pct(I["roi"]), pct(I["roe"]))))
    a(H2("（九）偿债能力分析"))
    a(P("运营期平均利息备付率%.2f，平均偿债备付率%.2f，资产负债率平均%s，低于70%%的警戒线。"
        "项目偿债能力较强，能够按期偿还贷款本息。"
        % (I["icr"], I["dscr"], pct(I["liability_ratio"] / 100))))
    a(H2("（十）盈亏平衡分析"))
    a(P("以生产能力利用率表示的盈亏平衡点为%.2f%%，低于70%%的安全线，项目抗风险能力较强。" % I["bep"]))
    a(H2("（十一）敏感性分析"))
    a({"t": "table", "rows": T["敏感性分析表"]})
    _s = T["敏感性分析表"][1:]
    _rm = [r for r in _s if r[0] == "营业收入" and r[1] == "-10%"][0]
    a(P("敏感性分析表明，营业收入影响最为显著，下降10%%时所得税后财务内部收益率降至%s，"
        "仍高于基准收益率；投资和成本变动10%%时，内部收益率在%s至%s之间。项目抗风险能力较强。"
        % (_rm[2], min(r[2] for r in _s), max(r[2] for r in _s))))
    a(H2("（十二）财务评价结论"))
    a(P("项目所得税后财务内部收益率%s，高于基准收益率；财务净现值大于零；投资回收期%.1f年，"
        "短于计算期；偿债能力指标良好；盈亏平衡点较低。项目财务上可行。"
        % (pct(I["firr_after_tax"]), I["payback_static"])))
    a({"t": "table", "rows": T["财务评价指标汇总表"]})

    a(H1("十、经济与社会效益评价"))
    a(P("经济效益方面，项目达产后年营业收入%.0f万元，可带动区域加工、包装、运输等关联产业发展。"
        "社会效益方面，项目可提供直接就业岗位并带动间接就业，降低流通损耗，提升应急保供和"
        "食品安全保障能力，促进农民增收和乡村振兴。" % annual_revenue))
    a(P("本项目以企业投资为主。如申请中央预算内投资或地方政府专项债券，需按《建设项目经济评价"
        "方法与参数》补充国民经济评价，测算影子价格下的经济内部收益率（EIRR）和经济净现值（ENPV）。"
        "【待补充】"))

    a(H1("十一、风险分析与对策"))
    a(P("市场风险：设施集中投放可能导致利用率不及预期，对策是开工前完成意向预租、签订长期协议。"
        "建设风险：工期延误和造价上涨，对策是采用工程总承包、限额设计和全过程造价咨询。"
        "运营风险：能源与人工成本上升、设备故障，对策是建设屋顶光伏、实施智能运维和预防性维护。"
        "资金风险：利率波动和还贷压力，对策是约定固定利率、分期提款并建立偿债准备金。"
        "政策风险：产业政策调整，对策是跟踪政策、提升市场化运营能力。综合评价，项目风险总体可控。"))

    a(H1("十二、结论与建议"))
    a(P("（一）结论。项目建设符合产业政策，建设规模与需求相匹配，场址条件良好，技术方案成熟；"
        "项目总投资%.0f万元，资金筹措方案落实；所得税后财务内部收益率%s，财务净现值%.1f万元，"
        "静态投资回收期%.1f年，偿债指标良好，财务上可行；社会效益显著，风险可控。"
        "项目建设是必要的、可行的。"
        % (ti, pct(I["firr_after_tax"]), I["fnpv_after_tax"], I["payback_static"])))
    a(P("（二）建议。一是加快前期审批手续；二是落实资本金和银行贷款，确保资金按进度到位；"
        "三是同步开展招商引资和客户预租；四是提前组建运营团队，建立标准化作业与温控体系；"
        "五是如需申请政府投资，及时补充国民经济评价和资金申请报告。"))
    a({"t": "attachment", "text": "附件：1.投资估算表"})
    a({"t": "attachment", "text": "　　　2.项目实施进度计划表"})
    a({"t": "attachment", "text": "　　　3.财务评价附表"})
    a({"t": "sign", "org": unit, "date": date})

    if not output_path:
        out_dir = os.path.join(_AI, "agent", "outputs")
        os.makedirs(out_dir, exist_ok=True)
        output_path = os.path.join(out_dir, project_name + "可行性研究报告.docx")
    res = tools.doc_write(output_path, "gongwen", B, project_name + "可行性研究报告")
    return ("%s\n关键指标：总投资%.0f万元，税后FIRR %s，FNPV %.1f万元，静态回收期%.1f年，"
            "ROI %s，ROE %s，ICR %.2f，DSCR %.2f，BEP %.2f%%。"
            % (res, ti, pct(I["firr_after_tax"]), I["fnpv_after_tax"], I["payback_static"],
               pct(I["roi"]), pct(I["roe"]), I["icr"], I["dscr"], I["bep"]))
