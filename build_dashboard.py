"""ETL pipeline + Excel & interactive HTML dashboard generator for cross-border payments."""

from __future__ import annotations

import json
import os
import pandas as pd


def load_and_clean(raw_dir: str) -> tuple[pd.DataFrame, pd.DataFrame, dict]:
    """Read raw CSVs, deduplicate, filter test/failed orders, and compute USD metrics."""
    merchants = pd.read_csv(os.path.join(raw_dir, "merchants.csv"))
    rates = pd.read_csv(os.path.join(raw_dir, "channel_rates.csv"))
    tx = pd.read_csv(os.path.join(raw_dir, "transactions.csv"))

    raw_tx_count = len(tx)
    # 1. Deduplicate orders
    tx = tx.drop_duplicates(subset=["order_id"], keep="first").copy()
    dedup_count = len(tx)

    # 2. Exclude test merchants
    real_merchants = merchants[merchants["is_test"] == 0].copy()
    tx = tx[tx["merchant_id"].isin(real_merchants["merchant_id"])].copy()

    # 3. Keep only SUCCESS transactions for TPV/Revenue
    tx_success = tx[tx["status"] == "SUCCESS"].copy()
    tx_success["tx_time"] = pd.to_datetime(tx_success["tx_time"])

    # Assign W01..W12 relative to earliest date
    min_date = tx_success["tx_time"].min().normalize()
    tx_success["week_num"] = ((tx_success["tx_time"].dt.normalize() - min_date).dt.days // 7) + 1
    tx_success = tx_success[(tx_success["week_num"] >= 1) & (tx_success["week_num"] <= 12)].copy()
    tx_success["week"] = tx_success["week_num"].apply(lambda w: f"W{w:02d}")

    # 4. Merge merchant info and channel/FX rates
    df = tx_success.merge(
        real_merchants[["merchant_id", "merchant_name", "industry", "country", "bd_owner"]],
        on="merchant_id",
        how="left",
    ).merge(
        rates[["week", "channel", "currency", "fx_to_usd", "cost_rate"]],
        on=["week", "channel", "currency"],
        how="left",
    )

    # 5. Compute USD financials
    df["amount_usd"] = (df["amount_orig"] * df["fx_to_usd"]).round(2)
    df["revenue_usd"] = (df["amount_usd"] * df["fee_rate_merchant"]).round(2)
    df["cost_usd"] = (df["amount_usd"] * df["cost_rate"]).round(2)
    df["gross_profit_usd"] = (df["revenue_usd"] - df["cost_usd"]).round(2)

    etl_stats = {
        "raw_tx": raw_tx_count,
        "after_dedup": dedup_count,
        "clean_success_tx": len(df),
    }
    return df, real_merchants, etl_stats


def build_summaries(df: pd.DataFrame, merchants: pd.DataFrame) -> dict[str, pd.DataFrame]:
    """Build weekly KPI, funnel, industry breakdown, and WoW merchant alert tables."""
    weekly = (
        df.groupby("week", as_index=False)
        .agg(
            tpv_usd=("amount_usd", "sum"),
            revenue_usd=("revenue_usd", "sum"),
            cost_usd=("cost_usd", "sum"),
            gross_profit_usd=("gross_profit_usd", "sum"),
            order_count=("order_id", "count"),
            active_merchants=("merchant_id", "nunique"),
        )
        .sort_values("week")
    )
    weekly["take_rate_pct"] = (weekly["revenue_usd"] / weekly["tpv_usd"] * 100).round(3)
    weekly["gross_margin_pct"] = (weekly["gross_profit_usd"] / weekly["revenue_usd"] * 100).round(2)

    # Funnel metrics (overall 12-week cohort)
    total_reg = len(merchants)
    kyc_passed = int((merchants["kyc_status"] == "PASSED").sum())
    tx_counts = df.groupby("merchant_id")["order_id"].count()
    first_order = int((tx_counts >= 1).sum())
    repeat_active = int((tx_counts >= 5).sum())

    funnel = pd.DataFrame(
        [
            {"stage": "1. 注册商户", "count": total_reg, "conv_from_prev_pct": 100.0},
            {"stage": "2. KYC审核通过", "count": kyc_passed, "conv_from_prev_pct": round(kyc_passed / total_reg * 100, 1)},
            {"stage": "3. 首单激活交易", "count": first_order, "conv_from_prev_pct": round(first_order / max(kyc_passed, 1) * 100, 1)},
            {"stage": "4. 稳定复购(>=5单)", "count": repeat_active, "conv_from_prev_pct": round(repeat_active / max(first_order, 1) * 100, 1)},
        ]
    )

    # Industry breakdown
    industry = (
        df.groupby("industry", as_index=False)
        .agg(
            tpv_usd=("amount_usd", "sum"),
            revenue_usd=("revenue_usd", "sum"),
            gross_profit_usd=("gross_profit_usd", "sum"),
            active_merchants=("merchant_id", "nunique"),
            orders=("order_id", "count"),
        )
        .sort_values("tpv_usd", ascending=False)
    )
    industry["gross_margin_pct"] = (industry["gross_profit_usd"] / industry["revenue_usd"] * 100).round(2)

    # WoW Alert for latest two weeks (W11 vs W12)
    weeks = sorted(df["week"].unique())
    prev_w, curr_w = weeks[-2], weeks[-1]
    w_prev = df[df["week"] == prev_w].groupby("merchant_id")["amount_usd"].sum().rename("prev_tpv")
    w_curr = df[df["week"] == curr_w].groupby("merchant_id")["amount_usd"].sum().rename("curr_tpv")

    alert = (
        merchants[["merchant_id", "merchant_name", "industry", "bd_owner"]]
        .merge(w_prev, on="merchant_id", how="inner")
        .merge(w_curr, on="merchant_id", how="left")
    )
    alert["curr_tpv"] = alert["curr_tpv"].fillna(0.0)
    alert["wow_pct"] = ((alert["curr_tpv"] - alert["prev_tpv"]) / alert["prev_tpv"] * 100).round(1)
    alert = alert[alert["prev_tpv"] >= 3000].sort_values("wow_pct").head(10).reset_index(drop=True)

    return {
        "weekly": weekly,
        "funnel": funnel,
        "industry": industry,
        "alert": alert,
    }


def export_excel(df: pd.DataFrame, summaries: dict[str, pd.DataFrame], out_path: str) -> None:
    """Export clean flat table and pre-aggregated sheets with an embedded Excel chart."""
    with pd.ExcelWriter(out_path, engine="xlsxwriter") as writer:
        summaries["weekly"].to_excel(writer, sheet_name="KPI_Weekly", index=False)
        summaries["funnel"].to_excel(writer, sheet_name="Funnel_Summary", index=False)
        summaries["industry"].to_excel(writer, sheet_name="Industry_Breakdown", index=False)
        summaries["alert"].to_excel(writer, sheet_name="Merchant_WoW_Alert", index=False)

        clean_cols = [
            "order_id", "week", "tx_time", "merchant_id", "merchant_name",
            "industry", "country", "bd_owner", "channel", "currency",
            "amount_orig", "fx_to_usd", "amount_usd", "fee_rate_merchant",
            "revenue_usd", "cost_rate", "cost_usd", "gross_profit_usd",
        ]
        df[clean_cols].to_excel(writer, sheet_name="Clean_Transactions", index=False)

        wb = writer.book
        ws = writer.sheets["KPI_Weekly"]
        n_rows = len(summaries["weekly"])

        chart = wb.add_chart({"type": "column"})
        chart.add_series({
            "name": "周交易额 (USD)",
            "categories": ["KPI_Weekly", 1, 0, n_rows, 0],
            "values": ["KPI_Weekly", 1, 1, n_rows, 1],
            "fill": {"color": "#1B365D"},
        })
        line_chart = wb.add_chart({"type": "line"})
        line_chart.add_series({
            "name": "毛利率 (%)",
            "categories": ["KPI_Weekly", 1, 0, n_rows, 0],
            "values": ["KPI_Weekly", 1, 8, n_rows, 8],
            "y2_axis": True,
            "line": {"color": "#D9534F", "width": 2.25},
        })
        chart.combine(line_chart)
        chart.set_title({"name": "周度交易额 (TPV) 与毛利率趋势"})
        chart.set_size({"width": 720, "height": 360})
        ws.insert_chart("K2", chart)


def export_html(df: pd.DataFrame, merchants: pd.DataFrame, summaries: dict[str, pd.DataFrame], etl_stats: dict, out_path: str) -> None:
    """Generate a self-contained interactive HTML dashboard with zero external dependencies."""
    # Pre-aggregate compact JSON for instant client-side filtering by industry & currency
    agg = (
        df.groupby(["week", "industry", "currency", "channel"], as_index=False)
        .agg(
            tpv=("amount_usd", "sum"),
            rev=("revenue_usd", "sum"),
            gp=("gross_profit_usd", "sum"),
            orders=("order_id", "count"),
        )
    )
    merchant_week = (
        df.groupby(["week", "industry", "currency", "merchant_id"], as_index=False)
        .agg(tpv=("amount_usd", "sum"))
    )
    funnel_records = summaries["funnel"].to_dict(orient="records")
    alert_records = summaries["alert"].to_dict(orient="records")

    payload = {
        "agg": agg.to_dict(orient="records"),
        "merchant_week": merchant_week.to_dict(orient="records"),
        "funnel": funnel_records,
        "alert": alert_records,
        "industries": sorted(df["industry"].unique().tolist()),
        "currencies": sorted(df["currency"].unique().tolist()),
        "weeks": sorted(df["week"].unique().tolist()),
        "etl": etl_stats,
    }

    html_template = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8" />
<meta name="viewport" content="width=device-width, initial-scale=1.0" />
<title>跨境支付经营分析看板 | Cross-Border Payment Dashboard</title>
<style>
  :root {
    --navy: #1b365d;
    --teal: #0d9488;
    --coral: #dc2626;
    --amber: #d97706;
    --bg: #f4f6f9;
    --card: #ffffff;
    --border: #e2e8f0;
    --text: #1e293b;
    --muted: #64748b;
  }
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body { font-family: -apple-system, BlinkMacSystemFont, "PingFang SC", "Microsoft YaHei", sans-serif; background: var(--bg); color: var(--text); padding: 20px; line-height: 1.5; }
  .container { max-width: 1320px; margin: 0 auto; }
  .header { display: flex; flex-wrap: wrap; justify-content: space-between; align-items: center; gap: 16px; margin-bottom: 18px; }
  .title-block h1 { font-size: 22px; color: var(--navy); font-weight: 700; }
  .title-block p { font-size: 13px; color: var(--muted); margin-top: 2px; }
  .filters { display: flex; gap: 10px; flex-wrap: wrap; align-items: center; }
  select { padding: 7px 12px; border: 1px solid var(--border); border-radius: 6px; background: #fff; font-size: 13px; color: var(--text); cursor: pointer; }
  .kpi-grid { display: grid; grid-template-columns: repeat(4, 1fr); gap: 14px; margin-bottom: 16px; }
  .card { background: var(--card); border: 1px solid var(--border); border-radius: 10px; padding: 16px 18px; box-shadow: 0 1px 2px rgba(15,23,42,0.04); }
  .kpi-label { font-size: 12px; color: var(--muted); font-weight: 600; text-transform: uppercase; letter-spacing: .3px; }
  .kpi-value { font-size: 26px; font-weight: 700; color: var(--navy); margin: 6px 0 4px; }
  .kpi-delta { font-size: 12px; font-weight: 600; }
  .up { color: var(--teal); }
  .down { color: var(--coral); }
  .main-grid { display: grid; grid-template-columns: 2fr 1fr; gap: 16px; margin-bottom: 16px; }
  .sub-grid { display: grid; grid-template-columns: 1fr 1.3fr; gap: 16px; margin-bottom: 16px; }
  .card-title { font-size: 15px; font-weight: 700; color: var(--navy); margin-bottom: 12px; display: flex; justify-content: space-between; align-items: center; }
  .card-sub { font-size: 12px; color: var(--muted); font-weight: 400; }
  .exec-box { background: #f8fafc; border-left: 4px solid var(--navy); padding: 14px 16px; border-radius: 6px; margin-bottom: 16px; }
  .exec-box h3 { font-size: 14px; color: var(--navy); margin-bottom: 8px; }
  .exec-box ul { padding-left: 18px; font-size: 13px; color: #334155; }
  .exec-box li { margin-bottom: 5px; }
  .bar-row { margin-bottom: 11px; }
  .bar-meta { display: flex; justify-content: space-between; font-size: 13px; margin-bottom: 4px; }
  .bar-track { height: 10px; background: #e2e8f0; border-radius: 99px; overflow: hidden; }
  .bar-fill { height: 100%; background: var(--navy); border-radius: 99px; transition: width .3s; }
  .funnel-step { display: flex; align-items: center; gap: 12px; margin-bottom: 12px; }
  .funnel-label { width: 135px; font-size: 13px; font-weight: 600; }
  .funnel-bar-wrap { flex: 1; background: #eef2f7; border-radius: 6px; height: 26px; position: relative; overflow: hidden; }
  .funnel-bar { height: 100%; background: linear-gradient(90deg, #1b365d, #2b548f); color: #fff; font-size: 12px; display: flex; align-items: center; padding-left: 10px; font-weight: 600; }
  .funnel-rate { width: 72px; text-align: right; font-size: 12px; color: var(--muted); }
  table { width: 100%; border-collapse: collapse; font-size: 13px; }
  th { text-align: left; padding: 9px 8px; border-bottom: 2px solid var(--border); color: var(--muted); font-weight: 600; font-size: 12px; }
  td { padding: 9px 8px; border-bottom: 1px solid #f1f5f9; }
  .badge-warn { background: #fee2e2; color: #b91c1c; padding: 2px 7px; border-radius: 99px; font-size: 12px; font-weight: 600; }
  .svg-wrap { width: 100%; overflow-x: auto; }
  .tooltip { position: fixed; background: rgba(15,23,42,0.92); color: #fff; padding: 7px 10px; border-radius: 6px; font-size: 12px; pointer-events: none; display: none; z-index: 99; }
  @media (max-width: 960px) {
    .kpi-grid { grid-template-columns: repeat(2, 1fr); }
    .main-grid, .sub-grid { grid-template-columns: 1fr; }
  }
</style>
</head>
<body>
<div class="container">
  <div class="header">
    <div class="title-block">
      <h1>跨境支付周度经营分析看板 (Demo)</h1>
      <p id="etl-info"></p>
    </div>
    <div class="filters">
      <label style="font-size:13px;color:var(--muted);">行业：</label>
      <select id="f-industry"><option value="ALL">全部行业</option></select>
      <label style="font-size:13px;color:var(--muted);">币种：</label>
      <select id="f-currency"><option value="ALL">全部币种</option></select>
      <label style="font-size:13px;color:var(--muted);">对比周：</label>
      <select id="f-week"></select>
    </div>
  </div>

  <div class="exec-box">
    <h3>管理层一分钟摘要（Executive Summary）</h3>
    <ul id="exec-list"></ul>
  </div>

  <div class="kpi-grid">
    <div class="card"><div class="kpi-label">当周交易额 (TPV)</div><div class="kpi-value" id="kpi-tpv">-</div><div class="kpi-delta" id="kpi-tpv-d">-</div></div>
    <div class="card"><div class="kpi-label">净收入 &amp; 综合费率</div><div class="kpi-value" id="kpi-rev">-</div><div class="kpi-delta" id="kpi-rev-d">-</div></div>
    <div class="card"><div class="kpi-label">通道毛利率</div><div class="kpi-value" id="kpi-gm">-</div><div class="kpi-delta" id="kpi-gm-d">-</div></div>
    <div class="card"><div class="kpi-label">活跃交易商户数</div><div class="kpi-value" id="kpi-m">-</div><div class="kpi-delta" id="kpi-m-d">-</div></div>
  </div>

  <div class="main-grid">
    <div class="card">
      <div class="card-title"><span>12 周交易额 (TPV) 与毛利率趋势</span><span class="card-sub">柱状：TPV ($K) ｜ 折线：毛利率 (%)</span></div>
      <div class="svg-wrap"><svg id="trend-svg" viewBox="0 0 760 280" width="100%" height="280"></svg></div>
    </div>
    <div class="card">
      <div class="card-title"><span>分行业交易额与毛利率（12周累计）</span><span class="card-sub">按当前币种筛选</span></div>
      <div id="industry-bars"></div>
    </div>
  </div>

  <div class="sub-grid">
    <div class="card">
      <div class="card-title"><span>商户转化漏斗（12周全量）</span><span class="card-sub">注册 → KYC → 首单 → 复购</span></div>
      <div id="funnel-box"></div>
    </div>
    <div class="card">
      <div class="card-title"><span>重点商户流失预警 Top 10（W11 → W12）</span><span class="card-sub">上周 TPV ≥ $3,000 且环比下滑显著</span></div>
      <div style="overflow-x:auto;">
        <table>
          <thead><tr><th>商户名称</th><th>行业</th><th>负责人</th><th>W11 TPV</th><th>W12 TPV</th><th>环比变化</th></tr></thead>
          <tbody id="alert-tbody"></tbody>
        </table>
      </div>
    </div>
  </div>
</div>
<div class="tooltip" id="tip"></div>

<script>
const DATA = __PAYLOAD_JSON__;

const indSel = document.getElementById("f-industry");
const ccySel = document.getElementById("f-currency");
const wkSel  = document.getElementById("f-week");

DATA.industries.forEach(i => indSel.add(new Option(i, i)));
DATA.currencies.forEach(c => ccySel.add(new Option(c, c)));
DATA.weeks.forEach(w => wkSel.add(new Option(w, w)));
wkSel.value = DATA.weeks[DATA.weeks.length - 1];

document.getElementById("etl-info").textContent =
  `ETL 自动清洗：原始流水 ${DATA.etl.raw_tx.toLocaleString()} 条 → 去重后 ${DATA.etl.after_dedup.toLocaleString()} 条 → 剔除测试/失败单后有效样本 ${DATA.etl.clean_success_tx.toLocaleString()} 条`;

function fmtUSD(v) {
  if (v >= 1e6) return "$" + (v / 1e6).toFixed(2) + "M";
  if (v >= 1e3) return "$" + (v / 1e3).toFixed(1) + "K";
  return "$" + v.toFixed(0);
}

function deltaHTML(curr, prev, isPctPoint=false) {
  if (prev === null || prev === undefined || prev === 0) return '<span style="color:var(--muted)">无上期对比</span>';
  const diff = isPctPoint ? (curr - prev) : ((curr - prev) / prev * 100);
  const sign = diff >= 0 ? "▲ +" : "▼ ";
  const cls = diff >= 0 ? "up" : "down";
  const unit = isPctPoint ? " pct" : "%";
  return `<span class="${cls}">${sign}${diff.toFixed(2)}${unit} 环比上周</span>`;
}

function render() {
  const ind = indSel.value;
  const ccy = ccySel.value;
  const selW = wkSel.value;
  const wkIdx = DATA.weeks.indexOf(selW);
  const prevW = wkIdx > 0 ? DATA.weeks[wkIdx - 1] : null;

  const filtered = DATA.agg.filter(r => (ind === "ALL" || r.industry === ind) && (ccy === "ALL" || r.currency === ccy));
  const byWeek = {};
  DATA.weeks.forEach(w => byWeek[w] = { tpv: 0, rev: 0, gp: 0, orders: 0, merchants: new Set() });
  filtered.forEach(r => {
    byWeek[r.week].tpv += r.tpv;
    byWeek[r.week].rev += r.rev;
    byWeek[r.week].gp += r.gp;
    byWeek[r.week].orders += r.orders;
  });
  DATA.merchant_week
    .filter(r => (ind === "ALL" || r.industry === ind) && (ccy === "ALL" || r.currency === ccy))
    .forEach(r => byWeek[r.week].merchants.add(r.merchant_id));

  const cD = byWeek[selW];
  const pD = prevW ? byWeek[prevW] : null;
  const cGM = cD.rev ? (cD.gp / cD.rev * 100) : 0;
  const pGM = (pD && pD.rev) ? (pD.gp / pD.rev * 100) : null;
  const cTR = cD.tpv ? (cD.rev / cD.tpv * 100) : 0;

  document.getElementById("kpi-tpv").textContent = fmtUSD(cD.tpv);
  document.getElementById("kpi-tpv-d").innerHTML = deltaHTML(cD.tpv, pD ? pD.tpv : null);
  document.getElementById("kpi-rev").textContent = `${fmtUSD(cD.rev)} (${cTR.toFixed(2)}%)`;
  document.getElementById("kpi-rev-d").innerHTML = deltaHTML(cD.rev, pD ? pD.rev : null);
  document.getElementById("kpi-gm").textContent = cGM.toFixed(1) + "%";
  document.getElementById("kpi-gm-d").innerHTML = deltaHTML(cGM, pGM, true);
  document.getElementById("kpi-m").textContent = cD.merchants.size;
  document.getElementById("kpi-m-d").innerHTML = deltaHTML(cD.merchants.size, pD ? pD.merchants.size : null);

  // Executive Summary
  const execItems = [];
  if (pD && pD.tpv > 0) {
    const g = ((cD.tpv - pD.tpv) / pD.tpv * 100).toFixed(1);
    execItems.push(`<strong>规模波动：</strong>${selW} 交易额 ${fmtUSD(cD.tpv)}，环比 ${prevW} ${g >= 0 ? "增长 +" : "下降 "}${g}%。`);
  } else {
    execItems.push(`<strong>规模表现：</strong>${selW} 交易额 ${fmtUSD(cD.tpv)}，活跃商户 ${cD.merchants.size} 家。`);
  }
  // Check Channel_B_SEA margin drop in W10-W12
  const seaRows = filtered.filter(r => r.week === selW && r.channel === "Channel_B_SEA");
  const seaRev = seaRows.reduce((s, r) => s + r.rev, 0);
  const seaGP  = seaRows.reduce((s, r) => s + r.gp, 0);
  if (seaRev > 0) {
    execItems.push(`<strong>通道结构：</strong>东南亚通道（Channel_B_SEA）当周毛利率为 <strong>${(seaGP/seaRev*100).toFixed(1)}%</strong>（W10起通道费率上调影响利润率，建议复核 MYR/THB 定价）。`);
  }
  const topDrop = DATA.alert[0];
  execItems.push(`<strong>流失预警：</strong>W12 共有 ${DATA.alert.length} 家重点商户交易额下滑显著，其中 <strong>${topDrop.merchant_name}</strong>（${topDrop.bd_owner}）环比下降 ${topDrop.wow_pct}%，建议本周优先回访。`);
  document.getElementById("exec-list").innerHTML = execItems.map(x => `<li>${x}</li>`).join("");

  // Trend SVG
  const svg = document.getElementById("trend-svg");
  const W = 760, H = 280, padL = 54, padR = 46, padT = 20, padB = 36;
  const cw = W - padL - padR, ch = H - padT - padB;
  const tpvs = DATA.weeks.map(w => byWeek[w].tpv);
  const gms  = DATA.weeks.map(w => byWeek[w].rev ? (byWeek[w].gp / byWeek[w].rev * 100) : 0);
  const maxT = Math.max(...tpvs, 1) * 1.15;
  const minG = Math.max(0, Math.floor(Math.min(...gms) - 5));
  const maxG = Math.min(100, Math.ceil(Math.max(...gms) + 5));
  const stepX = cw / DATA.weeks.length;

  let out = "";
  for (let i = 0; i <= 4; i++) {
    const y = padT + ch - (ch * i / 4);
    out += `<line x1="${padL}" y1="${y}" x2="${W-padR}" y2="${y}" stroke="#e2e8f0" stroke-dasharray="3,3"/>`;
    out += `<text x="${padL-6}" y="${y+4}" text-anchor="end" font-size="11" fill="#64748b">${fmtUSD(maxT*i/4)}</text>`;
    const gVal = minG + (maxG - minG) * i / 4;
    out += `<text x="${W-padR+6}" y="${y+4}" text-anchor="start" font-size="11" fill="#dc2626">${gVal.toFixed(0)}%</text>`;
  }
  let linePts = [];
  DATA.weeks.forEach((w, idx) => {
    const x = padL + idx * stepX + stepX * 0.2;
    const bw = stepX * 0.6;
    const bh = (tpvs[idx] / maxT) * ch;
    const y = padT + ch - bh;
    const fill = w === selW ? "#0d9488" : "#1b365d";
    out += `<rect class="tbar" data-w="${w}" data-tpv="${fmtUSD(tpvs[idx])}" data-gm="${gms[idx].toFixed(1)}%" x="${x}" y="${y}" width="${bw}" height="${bh}" rx="3" fill="${fill}" opacity="0.9"/>`;
    out += `<text x="${x+bw/2}" y="${H-12}" text-anchor="middle" font-size="11" fill="#475569">${w}</text>`;
    const gy = padT + ch - ((gms[idx] - minG) / Math.max(maxG - minG, 1)) * ch;
    linePts.push(`${x+bw/2},${gy}`);
  });
  out += `<polyline fill="none" stroke="#dc2626" stroke-width="2.4" points="${linePts.join(" ")}"/>`;
  linePts.forEach(p => {
    const [cx, cy] = p.split(",");
    out += `<circle cx="${cx}" cy="${cy}" r="3.5" fill="#fff" stroke="#dc2626" stroke-width="2"/>`;
  });
  svg.innerHTML = out;

  const tip = document.getElementById("tip");
  svg.querySelectorAll(".tbar").forEach(el => {
    el.addEventListener("mousemove", e => {
      tip.style.display = "block";
      tip.style.left = (e.clientX + 12) + "px";
      tip.style.top  = (e.clientY - 28) + "px";
      tip.textContent = `${el.dataset.w} ｜ TPV: ${el.dataset.tpv} ｜ 毛利率: ${el.dataset.gm}`;
    });
    el.addEventListener("mouseleave", () => tip.style.display = "none");
  });

  // Industry bars (filtered by currency only so comparison remains meaningful)
  const ccyFiltered = DATA.agg.filter(r => ccy === "ALL" || r.currency === ccy);
  const indMap = {};
  ccyFiltered.forEach(r => {
    if (!indMap[r.industry]) indMap[r.industry] = { tpv: 0, rev: 0, gp: 0 };
    indMap[r.industry].tpv += r.tpv;
    indMap[r.industry].rev += r.rev;
    indMap[r.industry].gp  += r.gp;
  });
  const indArr = Object.entries(indMap).map(([k, v]) => ({ name: k, ...v })).sort((a,b) => b.tpv - a.tpv);
  const maxInd = indArr.length ? indArr[0].tpv : 1;
  document.getElementById("industry-bars").innerHTML = indArr.map(x => {
    const gm = x.rev ? (x.gp / x.rev * 100).toFixed(1) : "0.0";
    const wPct = (x.tpv / maxInd * 100).toFixed(1);
    return `<div class="bar-row">
      <div class="bar-meta"><span><strong>${x.name}</strong></span><span>${fmtUSD(x.tpv)} · 毛利率 ${gm}%</span></div>
      <div class="bar-track"><div class="bar-fill" style="width:${wPct}%"></div></div>
    </div>`;
  }).join("");
}

// Static Funnel & Alert Table
const maxF = DATA.funnel[0].count;
document.getElementById("funnel-box").innerHTML = DATA.funnel.map(f => {
  const pct = (f.count / maxF * 100).toFixed(1);
  return `<div class="funnel-step">
    <div class="funnel-label">${f.stage}</div>
    <div class="funnel-bar-wrap"><div class="funnel-bar" style="width:${pct}%">${f.count} 家</div></div>
    <div class="funnel-rate">转化 ${f.conv_from_prev_pct}%</div>
  </div>`;
}).join("");

document.getElementById("alert-tbody").innerHTML = DATA.alert.map(a => `
  <tr>
    <td><strong>${a.merchant_name}</strong></td>
    <td>${a.industry}</td>
    <td>${a.bd_owner}</td>
    <td>${fmtUSD(a.prev_tpv)}</td>
    <td>${fmtUSD(a.curr_tpv)}</td>
    <td><span class="badge-warn">${a.wow_pct}%</span></td>
  </tr>
`).join("");

[indSel, ccySel, wkSel].forEach(el => el.addEventListener("change", render));
render();
</script>
</body>
</html>
"""
    html_out = html_template.replace("__PAYLOAD_JSON__", json.dumps(payload, ensure_ascii=False))
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(html_out)


def main() -> None:
    base_dir = os.path.dirname(os.path.abspath(__file__))
    raw_dir = os.path.join(base_dir, "raw_data")
    out_dir = os.path.join(base_dir, "output")
    os.makedirs(out_dir, exist_ok=True)

    df, merchants, etl_stats = load_and_clean(raw_dir)
    summaries = build_summaries(df, merchants)

    excel_path = os.path.join(out_dir, "clean_data.xlsx")
    html_path = os.path.join(out_dir, "dashboard.html")
    export_excel(df, summaries, excel_path)
    export_html(df, merchants, summaries, etl_stats, html_path)

    print("Dashboard build complete:")
    print(f"  ETL: {etl_stats}")
    print(f"  Excel: {excel_path}")
    print(f"  HTML:  {html_path}")


if __name__ == "__main__":
    main()
