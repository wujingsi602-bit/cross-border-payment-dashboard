"""Generate a realistic 12-week cross-border payment raw dataset for dashboard demo."""

from __future__ import annotations

import os
from datetime import datetime, timedelta
import numpy as np
import pandas as pd


def main() -> None:
    rng = np.random.default_rng(42)
    base_dir = os.path.dirname(os.path.abspath(__file__))
    raw_dir = os.path.join(base_dir, "raw_data")
    os.makedirs(raw_dir, exist_ok=True)

    end_date = datetime(2026, 11, 22)
    start_date = end_date - timedelta(weeks=12) + timedelta(days=1)

    industries = ["独立站电商", "B2B外贸", "数娱出海", "国际物流"]
    industry_weights = [0.35, 0.30, 0.20, 0.15]
    countries = ["US", "UK", "DE", "SG", "MY", "TH"]
    bd_owners = ["BD_张伟", "BD_李敏", "BD_陈浩", "BD_王芳"]

    # 1. Merchants table (180 real + 5 test accounts)
    n_real = 180
    merchant_rows = []
    for i in range(1, n_real + 1):
        mid = f"M{1000 + i}"
        ind = rng.choice(industries, p=industry_weights)
        cty = rng.choice(countries)
        bd = rng.choice(bd_owners)
        reg_offset = int(rng.integers(-60, 84))  # some registered before the 12-week window
        reg_date = start_date + timedelta(days=reg_offset)

        # ~82% pass KYC, ~18% pending/rejected (higher drop in recent weeks to create funnel story)
        kyc_prob = 0.84 if reg_offset < 56 else 0.68
        if rng.random() < kyc_prob:
            kyc_status = "PASSED"
            kyc_pass_date = (reg_date + timedelta(days=int(rng.integers(1, 6)))).strftime("%Y-%m-%d")
        else:
            kyc_status = rng.choice(["PENDING", "REJECTED"], p=[0.65, 0.35])
            kyc_pass_date = ""

        merchant_rows.append(
            {
                "merchant_id": mid,
                "merchant_name": f"{ind[:2]}客户_{i:03d}",
                "industry": ind,
                "country": cty,
                "bd_owner": bd,
                "register_date": reg_date.strftime("%Y-%m-%d"),
                "kyc_status": kyc_status,
                "kyc_pass_date": kyc_pass_date,
                "is_test": 0,
            }
        )

    # Add 5 test accounts (real data always has test accounts that ETL must filter)
    for t in range(1, 6):
        merchant_rows.append(
            {
                "merchant_id": f"TEST_{t:02d}",
                "merchant_name": f"内部测试商户_{t}",
                "industry": "独立站电商",
                "country": "US",
                "bd_owner": "BD_测试",
                "register_date": start_date.strftime("%Y-%m-%d"),
                "kyc_status": "PASSED",
                "kyc_pass_date": start_date.strftime("%Y-%m-%d"),
                "is_test": 1,
            }
        )

    merchants_df = pd.DataFrame(merchant_rows)
    merchants_df.to_csv(os.path.join(raw_dir, "merchants.csv"), index=False, encoding="utf-8-sig")

    # 2. Channel & FX rates table
    currencies = {
        "USD": 1.00,
        "EUR": 1.08,
        "GBP": 1.27,
        "SGD": 0.74,
        "MYR": 0.225,
        "THB": 0.029,
    }
    channels = {
        "Channel_A_Global": {"base_cost": 0.0028},
        "Channel_B_SEA": {"base_cost": 0.0036},
        "Channel_C_Direct": {"base_cost": 0.0022},
    }

    rate_rows = []
    for w in range(1, 13):
        week_label = f"W{w:02d}"
        for ch, cfg in channels.items():
            for ccy, fx in currencies.items():
                # Intentional anomaly: Channel_B_SEA cost rises in W10-W12
                extra_cost = 0.0014 if (ch == "Channel_B_SEA" and w >= 10) else 0.0
                fx_jitter = fx * (1 + rng.normal(0, 0.004))
                rate_rows.append(
                    {
                        "week": week_label,
                        "channel": ch,
                        "currency": ccy,
                        "fx_to_usd": round(float(fx_jitter), 4),
                        "cost_rate": round(cfg["base_cost"] + extra_cost, 5),
                    }
                )
    rates_df = pd.DataFrame(rate_rows)
    rates_df.to_csv(os.path.join(raw_dir, "channel_rates.csv"), index=False, encoding="utf-8-sig")

    # 3. Transactions table (~6,200 orders across 12 weeks)
    active_merchants = merchants_df[(merchants_df["kyc_status"] == "PASSED") & (merchants_df["is_test"] == 0)].copy()
    # Select ~125 merchants that actually transact
    transacting_ids = set(rng.choice(active_merchants["merchant_id"].values, size=125, replace=False))
    # Pick 6 "declining key merchants" in W12 for the alert table story
    declining_ids = set(list(transacting_ids)[:6])
    # Pick 8 "high-growth SEA/indie merchants" in W10-W12
    growing_ids = set(list(transacting_ids)[6:16])

    ind_fee_base = {
        "独立站电商": 0.0068,
        "B2B外贸": 0.0048,
        "数娱出海": 0.0085,
        "国际物流": 0.0055,
    }
    ind_amt_mean = {
        "独立站电商": 2200,
        "B2B外贸": 8500,
        "数娱出海": 1600,
        "国际物流": 4800,
    }

    tx_rows = []
    order_seq = 100001
    for w in range(1, 13):
        w_start = start_date + timedelta(weeks=w - 1)
        # Overall gentle upward trend
        n_orders_week = int(450 + w * 14 + rng.integers(-20, 25))
        for _ in range(n_orders_week):
            m_row = active_merchants[active_merchants["merchant_id"].isin(transacting_ids)].sample(
                1, random_state=int(rng.integers(0, 1_000_000))
            ).iloc[0]
            mid = m_row["merchant_id"]
            ind = m_row["industry"]

            # Skip some orders for declining merchants in W12
            if mid in declining_ids and w == 12 and rng.random() < 0.65:
                continue

            tx_dt = w_start + timedelta(
                days=int(rng.integers(0, 7)),
                hours=int(rng.integers(0, 24)),
                minutes=int(rng.integers(0, 60)),
            )
            # Only generate tx after kyc_pass_date
            if tx_dt.strftime("%Y-%m-%d") < m_row["kyc_pass_date"]:
                continue

            ccy = rng.choice(["USD", "EUR", "GBP", "SGD", "MYR", "THB"], p=[0.44, 0.18, 0.10, 0.10, 0.10, 0.08])
            if ccy in ("MYR", "THB", "SGD"):
                ch = rng.choice(["Channel_B_SEA", "Channel_A_Global"], p=[0.75, 0.25])
            else:
                ch = rng.choice(["Channel_A_Global", "Channel_C_Direct"], p=[0.55, 0.45])

            usd_target = float(rng.lognormal(mean=np.log(ind_amt_mean[ind]), sigma=0.55))
            if mid in growing_ids and w >= 10:
                usd_target *= 1.45
            if mid in declining_ids and w == 12:
                usd_target *= 0.42

            amt_orig = round(usd_target / currencies[ccy], 2)
            fee_rate = round(float(ind_fee_base[ind] + rng.normal(0, 0.0005)), 5)
            status = rng.choice(["SUCCESS", "FAILED", "REFUNDED"], p=[0.93, 0.05, 0.02])

            tx_rows.append(
                {
                    "order_id": f"ORD{order_seq}",
                    "merchant_id": mid,
                    "tx_time": tx_dt.strftime("%Y-%m-%d %H:%M:%S"),
                    "currency": ccy,
                    "amount_orig": amt_orig,
                    "fee_rate_merchant": max(fee_rate, 0.0025),
                    "channel": ch,
                    "status": status,
                }
            )
            order_seq += 1

    # Add 25 test transactions & 15 duplicate rows for ETL realism
    for t in range(1, 26):
        tx_rows.append(
            {
                "order_id": f"ORD_TEST_{t:03d}",
                "merchant_id": f"TEST_{(t % 5) + 1:02d}",
                "tx_time": (end_date - timedelta(days=int(rng.integers(1, 30)))).strftime("%Y-%m-%d %H:%M:%S"),
                "currency": "USD",
                "amount_orig": 99999.0,
                "fee_rate_merchant": 0.005,
                "channel": "Channel_A_Global",
                "status": "SUCCESS",
            }
        )
    tx_df = pd.DataFrame(tx_rows)
    dups = tx_df.sample(15, random_state=42)
    tx_df = pd.concat([tx_df, dups], ignore_index=True)
    tx_df.to_csv(os.path.join(raw_dir, "transactions.csv"), index=False, encoding="utf-8-sig")

    print(f"Generated raw data in {raw_dir}:")
    print(f"  merchants.csv: {len(merchants_df)} rows")
    print(f"  channel_rates.csv: {len(rates_df)} rows")
    print(f"  transactions.csv: {len(tx_df)} rows")


if __name__ == "__main__":
    main()
