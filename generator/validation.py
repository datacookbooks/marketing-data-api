"""Business-rule, experiment, key, date, and defect validations."""

from __future__ import annotations

import math
import re
from typing import Dict

import numpy as np
import pandas as pd


TABLE_GRAINS = {
    "dim_customer": "One row per registered customer",
    "dim_plan": "One row per subscription plan",
    "dim_campaign": "One row per marketing campaign",
    "fact_subscription_period": "One row per continuous customer-plan period",
    "fact_payment": "One row per payment attempt, charge, or refund",
    "fact_campaign_daily": "One row per campaign and calendar date",
    "fact_campaign_assignment": "One eligible person per campaign measurement window",
}

DATA_DICTIONARY = {
    "dim_customer": {
        "customer_id": "Stable registered-customer key", "prospect_key": "Pre-signup person key",
        "signup_timestamp": "First registration timestamp", "initial_acquisition_channel": "First known channel",
    },
    "dim_plan": {"plan_id": "Plan key", "monthly_price": "Standard monthly price", "is_paid_plan": "Paid-plan flag"},
    "dim_campaign": {"campaign_id": "Campaign key", "objective": "Acquisition or free-to-paid objective"},
    "fact_subscription_period": {"subscription_period_id": "Continuous-plan-period key", "end_reason": "Why the period ended"},
    "fact_payment": {"payment_id": "Payment-event business key", "attempt_number": "Attempt number within a cycle"},
    "fact_campaign_daily": {"metric_date": "Platform metric date", "spend": "Daily campaign cost"},
    "fact_campaign_assignment": {"assignment_id": "Experiment assignment key", "assignment_arm": "Treatment or holdout"},
}


def _check(name: str, passed: bool, observed: object, expectation: str) -> dict:
    return {"check_name": name, "status": "PASS" if passed else "FAIL", "observed": observed, "expectation": expectation}


def validate_clean_tables(tables: Dict[str, pd.DataFrame]) -> pd.DataFrame:
    checks: list[dict] = []
    required = set(TABLE_GRAINS)
    missing = sorted(required - set(tables))
    checks.append(_check("all seven tables present", not missing, ", ".join(missing) or "none missing", "no missing core tables"))
    if missing:
        return pd.DataFrame(checks)

    key_specs = {
        "dim_customer": ["customer_id"], "dim_plan": ["plan_id"], "dim_campaign": ["campaign_id"],
        "fact_subscription_period": ["subscription_period_id"], "fact_payment": ["payment_id"],
        "fact_campaign_daily": ["metric_date", "campaign_id"], "fact_campaign_assignment": ["assignment_id"],
    }
    for name, keys in key_specs.items():
        duplicates = int(tables[name].duplicated(keys).sum())
        checks.append(_check(f"{name} business key unique", duplicates == 0, duplicates, "0 duplicate keys"))

    customer_ids = set(tables["dim_customer"]["customer_id"])
    plan_ids = set(tables["dim_plan"]["plan_id"])
    campaign_ids = set(tables["dim_campaign"]["campaign_id"])
    period_ids = set(tables["fact_subscription_period"]["subscription_period_id"])
    checks.extend([
        _check("subscription customer FK", set(tables["fact_subscription_period"]["customer_id"]) <= customer_ids, "validated", "all customers match"),
        _check("subscription plan FK", set(tables["fact_subscription_period"]["plan_id"]) <= plan_ids, "validated", "all plans match"),
        _check("payment period FK", set(tables["fact_payment"]["subscription_period_id"]) <= period_ids, "validated", "all periods match"),
        _check("assignment campaign FK", set(tables["fact_campaign_assignment"]["campaign_id"]) <= campaign_ids, "validated", "all campaigns match"),
    ])

    periods = tables["fact_subscription_period"].sort_values(["customer_id", "period_start_timestamp"]).copy()
    periods["period_start_timestamp"] = pd.to_datetime(
        periods["period_start_timestamp"], utc=True
    )
    periods["period_end_timestamp"] = pd.to_datetime(
        periods["period_end_timestamp"], utc=True
    )
    invalid_dates = int(
        (
            periods["period_end_timestamp"].notna()
            & (
                periods["period_end_timestamp"]
                <= periods["period_start_timestamp"]
            )
        ).sum()
    )
    periods["previous_end"] = periods.groupby("customer_id")[
        "period_end_timestamp"
    ].shift()
    overlap = int(
        (
            periods["previous_end"].notna()
            & (
                periods["period_start_timestamp"]
                < periods["previous_end"]
            )
        ).sum()
    )
    checks.extend([
        _check("subscription date order", invalid_dates == 0, invalid_dates, "end is after start"),
        _check("subscription periods do not overlap", overlap == 0, overlap, "0 overlaps"),
    ])

    assignments = tables["fact_campaign_assignment"].copy()
    arm_counts = assignments.groupby(["prospect_key", "campaign_id", "measurement_window_id"])["assignment_arm"].nunique()
    arm_switches = int((arm_counts > 1).sum())
    checks.append(_check("fixed experiment assignment", arm_switches == 0, arm_switches, "0 people switch arms in a window"))

    payments = tables["fact_payment"]
    bad_refunds = int(((payments["payment_type"] == "refund") & (payments["amount"] >= 0)).sum())
    bad_charges = int(((payments["payment_type"] != "refund") & (payments["amount"] < 0)).sum())
    checks.extend([
        _check("refund signs", bad_refunds == 0, bad_refunds, "refund amounts are negative"),
        _check("charge signs", bad_charges == 0, bad_charges, "charge amounts are nonnegative"),
    ])
    return pd.DataFrame(checks)


def experiment_balance(tables: Dict[str, pd.DataFrame]) -> pd.DataFrame:
    assignments = tables["fact_campaign_assignment"]
    summary = assignments.groupby(["campaign_id", "measurement_window_id", "assignment_arm"]).size().unstack(fill_value=0)
    summary["treatment_share"] = summary.get("Treatment", 0) / summary.sum(axis=1)
    return summary.reset_index()


def campaign_lift_summary(truth_outcomes: pd.DataFrame, dim_campaign: pd.DataFrame) -> pd.DataFrame:
    rows: list[dict] = []
    for campaign_id, group in truth_outcomes.groupby("campaign_id"):
        treatment = group[group["assignment_arm"] == "Treatment"]["converted"].astype(int)
        holdout = group[group["assignment_arm"] == "Holdout"]["converted"].astype(int)
        treatment_rate = float(treatment.mean()) if len(treatment) else np.nan
        holdout_rate = float(holdout.mean()) if len(holdout) else np.nan
        lift = treatment_rate - holdout_rate
        pooled = (treatment.sum() + holdout.sum()) / (len(treatment) + len(holdout))
        se = math.sqrt(pooled * (1 - pooled) * (1 / len(treatment) + 1 / len(holdout))) if treatment.size and holdout.size else np.nan
        z = lift / se if se and not np.isnan(se) else np.nan
        p_value = math.erfc(abs(z) / math.sqrt(2)) if not np.isnan(z) else np.nan
        rows.append({
            "campaign_id": campaign_id,
            "treatment_n": len(treatment), "holdout_n": len(holdout),
            "treatment_conversion_rate": treatment_rate, "holdout_conversion_rate": holdout_rate,
            "absolute_lift": lift, "z_statistic": z, "two_sided_p_value": p_value,
        })
    result = pd.DataFrame(rows)
    return result.merge(dim_campaign[["campaign_id", "campaign_name"]], on="campaign_id", how="left")


def payment_recovery_summary(tables: Dict[str, pd.DataFrame]) -> pd.DataFrame:
    payments = tables["fact_payment"].copy()
    failed = payments[payments["payment_status"] == "failed"]
    retry = payments[(payments["payment_type"] == "retry") & (payments["attempt_number"] == 2)]
    return pd.DataFrame({
        "metric": ["failed attempts", "retry attempts", "successful retries", "retry recovery rate"],
        "value": [len(failed), len(retry), int((retry["payment_status"] == "succeeded").sum()), float((retry["payment_status"] == "succeeded").mean())],
    })


def audit_messy_tables(clean_tables: Dict[str, pd.DataFrame], raw_tables: Dict[str, pd.DataFrame]) -> pd.DataFrame:
    rows: list[dict] = []
    business_keys = {
        "dim_customer": ["customer_id"], "dim_plan": ["plan_id"], "dim_campaign": ["campaign_id"],
        "fact_subscription_period": ["subscription_period_id"], "fact_payment": ["payment_id"],
        "fact_campaign_daily": ["metric_date", "campaign_id"], "fact_campaign_assignment": ["assignment_id"],
    }
    for name, raw in raw_tables.items():
        keys = business_keys[name]
        duplicate_keys = int(raw.duplicated(keys, keep=False).sum())
        rows.append({
            "table_name": name, "clean_rows": len(clean_tables[name]), "raw_rows": len(raw),
            "duplicate_key_rows": duplicate_keys,
        })
    result = pd.DataFrame(rows)
    raw_payments = raw_tables["fact_payment"]
    result["invalid_numeric_rows"] = 0
    pay_invalid = ~raw_payments["amount"].fillna("").str.match(r"^-?\d+(\.\d+)?$")
    result.loc[result["table_name"] == "fact_payment", "invalid_numeric_rows"] = int(pay_invalid.sum())
    daily = raw_tables["fact_campaign_daily"]
    spend_invalid = ~daily["spend"].fillna("").str.match(r"^-?\d+(\.\d+)?$")
    result.loc[result["table_name"] == "fact_campaign_daily", "invalid_numeric_rows"] = int(spend_invalid.sum())
    return result
