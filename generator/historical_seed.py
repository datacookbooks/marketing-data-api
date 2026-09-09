"""Generate a deterministic, coherent historical business simulation.

The public result contains the seven analytical tables described in the
project plan. Campaign outcomes are kept separately as private simulation
truth so validation can confirm that the configured effects are recoverable.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
import hashlib
import math
from typing import Dict, Iterable

import numpy as np
import pandas as pd

try:
    from .config import DEFAULT_CONFIG, CampaignConfig, ProjectConfig
except ImportError:  # Support running the file directly.
    from config import DEFAULT_CONFIG, CampaignConfig, ProjectConfig


TABLE_ORDER = (
    "dim_plan",
    "dim_campaign",
    "dim_customer",
    "fact_subscription_period",
    "fact_payment",
    "fact_campaign_daily",
    "fact_campaign_assignment",
)


@dataclass
class SimulationResult:
    clean_tables: Dict[str, pd.DataFrame]
    truth_tables: Dict[str, pd.DataFrame]
    start_date: date
    end_date: date


def _stable_int(*parts: object, modulus: int = 10 ** 15) -> int:
    payload = "|".join(str(part) for part in parts).encode("utf-8")
    return int.from_bytes(hashlib.blake2b(payload, digest_size=8).digest(), "big") % modulus


def _rng(config: ProjectConfig, *parts: object) -> np.random.Generator:
    return np.random.default_rng(_stable_int(config.random_seed, *parts, modulus=2**32 - 1))


def _utc_timestamp(day: date, rng: np.random.Generator, hour_low: int = 8, hour_high: int = 22) -> pd.Timestamp:
    hour = int(rng.integers(hour_low, hour_high))
    minute = int(rng.integers(0, 60))
    return pd.Timestamp(datetime(day.year, day.month, day.day, hour, minute, tzinfo=timezone.utc))


def _quarter_id(day: date) -> str:
    return f"{day.year}_Q{((day.month - 1) // 3) + 1}"


def _date_factors(day: date, start_date: date, config: ProjectConfig) -> tuple[float, float, float]:
    years = (day - start_date).days / 365.25
    growth = (1.0 + config.annual_growth_rate) ** years
    weekday = 1.13 if day.weekday() in (5, 6) else 0.95
    month_factors = {
        1: 1.34, 2: 1.10, 3: 1.02, 4: 0.98, 5: 1.00, 6: 1.10,
        7: 1.15, 8: 1.07, 9: 1.03, 10: 1.00, 11: 0.92, 12: 0.78,
    }
    return growth, weekday, month_factors[day.month]


def _dimensions(config: ProjectConfig) -> tuple[pd.DataFrame, pd.DataFrame]:
    dim_plan = pd.DataFrame([
        {
            "plan_id": p.plan_id,
            "plan_name": p.plan_name,
            "monthly_price": p.monthly_price,
            "estimated_monthly_variable_cost": p.estimated_monthly_variable_cost,
            "weekly_recorded_lesson_limit": p.weekly_recorded_lesson_limit,
            "private_sessions_per_month": p.private_sessions_per_month,
            "is_paid_plan": p.is_paid_plan,
        }
        for p in config.plans
    ])
    dim_plan["weekly_recorded_lesson_limit"] = dim_plan["weekly_recorded_lesson_limit"].astype("Int64")
    dim_campaign = pd.DataFrame([
        {
            "campaign_id": c.campaign_id,
            "campaign_name": c.campaign_name,
            "channel": c.channel,
            "objective": c.objective,
            "is_evergreen": c.active_end_date is None,
            "active_start_date": pd.Timestamp(c.active_start_date),
            "active_end_date": pd.NaT if c.active_end_date is None else pd.Timestamp(c.active_end_date),
            "default_treatment_share": c.treatment_share,
        }
        for c in config.campaigns
    ])
    return dim_plan, dim_campaign


def _new_customer(
    prospect_key: str,
    signup_timestamp: pd.Timestamp,
    channel: str,
    first_campaign_id: int | None,
    initial_plan_id: int,
    config: ProjectConfig,
) -> dict:
    rng = _rng(config, "customer", prospect_key)
    states = list(config.state_weights)
    weights = np.array(list(config.state_weights.values()), dtype=float)
    weights /= weights.sum()
    customer_id = 100_000 + _stable_int("customer", prospect_key, modulus=10 ** 14)
    return {
        "customer_id": customer_id,
        "prospect_key": prospect_key,
        "email": f"learner{customer_id}@example.com",
        "signup_timestamp": signup_timestamp,
        "state": str(rng.choice(states, p=weights)),
        "experience_level": str(rng.choice(["Beginner", "Intermediate", "Advanced"], p=[0.62, 0.28, 0.10])),
        "initial_acquisition_channel": channel,
        "first_campaign_id": first_campaign_id,
        "source_updated_at": signup_timestamp + timedelta(hours=2),
        "_initial_plan_id": initial_plan_id,
        "_payment_risk": float(rng.beta(1.8, 20.0)),
        "_churn_risk": float(rng.lognormal(mean=-0.05, sigma=0.35)),
    }


def _acquisition_population(start_date: date, end_date: date, config: ProjectConfig):
    customers: list[dict] = []
    assignments: list[dict] = []
    outcomes: list[dict] = []
    acquisition_campaigns = [c for c in config.campaigns if c.objective == "Acquisition"]

    for day_ts in pd.date_range(start_date, end_date, freq="D"):
        day = day_ts.date()
        growth, weekday, seasonal = _date_factors(day, start_date, config)
        for campaign in acquisition_campaigns:
            rng = _rng(config, "acquisition", campaign.campaign_id, day.isoformat())
            population = int(rng.poisson(campaign.mean_daily_eligible * growth * weekday * seasonal))
            for position in range(population):
                prospect_key = f"P-{campaign.campaign_id}-{day:%Y%m%d}-{position:04d}"
                assignment_arm = "Treatment" if rng.random() < campaign.treatment_share else "Holdout"
                assigned_at = _utc_timestamp(day, rng, 6, 12)
                exposed = assignment_arm == "Treatment" and rng.random() < campaign.exposure_rate
                first_exposed_at = assigned_at + timedelta(hours=int(rng.integers(1, 18))) if exposed else pd.NaT
                probability = campaign.baseline_conversion_rate
                if assignment_arm == "Treatment":
                    probability += campaign.intention_to_treat_lift
                probability *= 1.0 + (0.06 if day.weekday() in (5, 6) else 0.0)
                converted = bool(rng.random() < min(probability, 0.80))
                customer_id = None
                conversion_timestamp = pd.NaT
                if converted:
                    lag_days = int(rng.integers(0, 8))
                    signup_day = day + timedelta(days=lag_days)
                    # Consume the same random draws even when the outcome falls
                    # beyond the current cutoff. This makes an earlier prefix
                    # identical when the simulation is extended later.
                    candidate_timestamp = _utc_timestamp(signup_day, rng)
                    if campaign.campaign_id == 101:
                        candidate_plan_id = int(rng.choice([1, 2, 3], p=[0.48, 0.43, 0.09]))
                    else:
                        candidate_plan_id = int(rng.choice([1, 2, 3], p=[0.66, 0.29, 0.05]))
                    if signup_day <= end_date:
                        conversion_timestamp = candidate_timestamp
                        plan_id = candidate_plan_id
                        first_campaign_id = campaign.campaign_id if exposed else None
                        customer = _new_customer(
                            prospect_key, conversion_timestamp, campaign.channel,
                            first_campaign_id, plan_id, config,
                        )
                        customer_id = customer["customer_id"]
                        customers.append(customer)
                    else:
                        converted = False
                assignments.append({
                    "assignment_id": f"A-{_stable_int('assignment', prospect_key, campaign.campaign_id, _quarter_id(day)):010d}",
                    "prospect_key": prospect_key,
                    "customer_id": customer_id,
                    "campaign_id": campaign.campaign_id,
                    "measurement_window_id": _quarter_id(day),
                    "assignment_arm": assignment_arm,
                    "assigned_at": assigned_at,
                    "first_exposed_at": first_exposed_at,
                    "source_updated_at": assigned_at + timedelta(days=1),
                })
                outcomes.append({
                    "assignment_id": assignments[-1]["assignment_id"],
                    "campaign_id": campaign.campaign_id,
                    "assignment_arm": assignment_arm,
                    "converted": converted,
                    "conversion_timestamp": conversion_timestamp,
                    "customer_id": customer_id,
                    "outcome_type": "registration",
                })

        organic_rng = _rng(config, "organic", day.isoformat())
        organic_n = int(organic_rng.poisson(config.base_daily_organic_signups * growth * weekday * seasonal))
        for position in range(organic_n):
            prospect_key = f"O-{day:%Y%m%d}-{position:04d}"
            signup_timestamp = _utc_timestamp(day, organic_rng)
            plan_id = int(organic_rng.choice([1, 2, 3], p=[0.68, 0.27, 0.05]))
            customers.append(_new_customer(prospect_key, signup_timestamp, "Organic", None, plan_id, config))

    return customers, assignments, outcomes


def _quarter_starts(start_date: date, end_date: date) -> Iterable[date]:
    current = date(start_date.year, ((start_date.month - 1) // 3) * 3 + 1, 1)
    while current <= end_date:
        yield max(current, start_date)
        month = current.month + 3
        current = date(current.year + (month - 1) // 12, ((month - 1) % 12) + 1, 1)


def _nurture_population(customers: list[dict], start_date: date, end_date: date, config: ProjectConfig):
    campaign = next(c for c in config.campaigns if c.campaign_id == 103)
    assignments: list[dict] = []
    outcomes: list[dict] = []
    converted_free_customers: set[int] = set()
    nurture_conversion: dict[int, tuple[pd.Timestamp, int]] = {}

    for window_start in _quarter_starts(start_date, end_date):
        rng = _rng(config, "nurture", window_start.isoformat())
        eligible = [
            c for c in customers
            if c["_initial_plan_id"] == 1
            and c["customer_id"] not in converted_free_customers
            and c["signup_timestamp"].date() <= window_start - timedelta(days=14)
        ]
        if not eligible:
            continue
        sample_size = min(len(eligible), max(30, int(round(len(eligible) * 0.35))))
        selected_positions = rng.choice(len(eligible), size=sample_size, replace=False)
        for position in selected_positions:
            customer = eligible[int(position)]
            customer_id = customer["customer_id"]
            assigned_at = _utc_timestamp(window_start, rng, 7, 11)
            arm = "Treatment" if rng.random() < campaign.treatment_share else "Holdout"
            exposed = arm == "Treatment" and rng.random() < campaign.exposure_rate
            first_exposed_at = assigned_at + timedelta(hours=int(rng.integers(1, 36))) if exposed else pd.NaT
            probability = campaign.baseline_conversion_rate + (
                campaign.intention_to_treat_lift if arm == "Treatment" else 0.0
            )
            converted = bool(rng.random() < probability)
            conversion_timestamp = pd.NaT
            if converted:
                lag_days = int(rng.integers(1, 22))
                conversion_day = window_start + timedelta(days=lag_days)
                candidate_timestamp = _utc_timestamp(conversion_day, rng)
                candidate_plan_id = 3 if rng.random() < 0.08 else 2
                if conversion_day <= end_date:
                    conversion_timestamp = candidate_timestamp
                    plan_id = candidate_plan_id
                    nurture_conversion[customer_id] = (conversion_timestamp, plan_id)
                    converted_free_customers.add(customer_id)
                    if exposed and customer["first_campaign_id"] is None:
                        customer["first_campaign_id"] = campaign.campaign_id
                else:
                    converted = False
            assignment_id = f"A-{_stable_int('assignment', customer['prospect_key'], campaign.campaign_id, _quarter_id(window_start)):010d}"
            assignments.append({
                "assignment_id": assignment_id,
                "prospect_key": customer["prospect_key"],
                "customer_id": customer_id,
                "campaign_id": campaign.campaign_id,
                "measurement_window_id": _quarter_id(window_start),
                "assignment_arm": arm,
                "assigned_at": assigned_at,
                "first_exposed_at": first_exposed_at,
                "source_updated_at": assigned_at + timedelta(days=1),
            })
            outcomes.append({
                "assignment_id": assignment_id,
                "campaign_id": campaign.campaign_id,
                "assignment_arm": arm,
                "converted": converted,
                "conversion_timestamp": conversion_timestamp,
                "customer_id": customer_id,
                "outcome_type": "free_to_paid",
            })
    return assignments, outcomes, nurture_conversion


def _subscription_history(customers: list[dict], nurture_conversion: dict, end_date: date, config: ProjectConfig):
    periods: list[dict] = []
    plan_by_id = config.plan_by_id()
    end_limit = pd.Timestamp(end_date, tz="UTC") + timedelta(days=1)

    for customer in customers:
        customer_id = customer["customer_id"]
        initial_start = customer["signup_timestamp"]
        initial_plan = customer["_initial_plan_id"]
        if initial_plan == 1:
            conversion = nurture_conversion.get(customer_id)
            free_end = conversion[0] if conversion else pd.NaT
            periods.append({
                "subscription_period_id": f"S-{_stable_int(customer_id, initial_start.isoformat(), 1):010d}",
                "customer_id": customer_id,
                "plan_id": 1,
                "period_start_timestamp": initial_start,
                "period_end_timestamp": free_end,
                "end_reason": "upgrade" if conversion else None,
                "source_updated_at": (free_end if conversion else initial_start) + timedelta(hours=3),
            })
            if not conversion:
                continue
            paid_start, current_plan = conversion
        else:
            paid_start, current_plan = initial_start, initial_plan

        rng = _rng(config, "lifecycle", customer_id)
        segment_start = paid_start
        review_date = paid_start
        tenure_month = 0
        while review_date < end_limit:
            next_review = review_date + pd.DateOffset(months=1)
            if next_review >= end_limit:
                periods.append({
                    "subscription_period_id": f"S-{_stable_int(customer_id, segment_start.isoformat(), current_plan):010d}",
                    "customer_id": customer_id,
                    "plan_id": current_plan,
                    "period_start_timestamp": segment_start,
                    "period_end_timestamp": pd.NaT,
                    "end_reason": None,
                    "source_updated_at": segment_start + timedelta(hours=3),
                })
                break

            base_churn = 0.058 if current_plan == 2 else 0.038
            tenure_factor = 1.45 if tenure_month < 3 else (1.10 if tenure_month < 6 else 0.78)
            payment_factor = 1.0 + 2.2 * customer["_payment_risk"]
            churn_probability = min(0.24, base_churn * tenure_factor * payment_factor * customer["_churn_risk"])
            transition_draw = rng.random()
            end_reason = None
            new_plan = current_plan
            if transition_draw < churn_probability:
                end_reason = "move to Free" if rng.random() < 0.58 else "cancellation"
                new_plan = 1 if end_reason == "move to Free" else 0
            elif current_plan == 2 and transition_draw < churn_probability + 0.018:
                end_reason, new_plan = "upgrade", 3
            elif current_plan == 3 and transition_draw < churn_probability + 0.028:
                end_reason, new_plan = "downgrade", 2

            if end_reason is None:
                tenure_month += 1
                review_date = next_review
                continue

            periods.append({
                "subscription_period_id": f"S-{_stable_int(customer_id, segment_start.isoformat(), current_plan):010d}",
                "customer_id": customer_id,
                "plan_id": current_plan,
                "period_start_timestamp": segment_start,
                "period_end_timestamp": next_review,
                "end_reason": end_reason,
                "source_updated_at": next_review + timedelta(hours=3),
            })
            if new_plan == 1:
                periods.append({
                    "subscription_period_id": f"S-{_stable_int(customer_id, next_review.isoformat(), 1):010d}",
                    "customer_id": customer_id,
                    "plan_id": 1,
                    "period_start_timestamp": next_review,
                    "period_end_timestamp": pd.NaT,
                    "end_reason": None,
                    "source_updated_at": next_review + timedelta(hours=3),
                })
                break
            if new_plan == 0:
                if rng.random() < 0.12:
                    reactivation = next_review + timedelta(days=int(rng.integers(30, 121)))
                    if reactivation < end_limit:
                        segment_start = reactivation
                        review_date = reactivation
                        tenure_month = 0
                        continue
                break
            current_plan = new_plan
            segment_start = next_review
            review_date = next_review
            tenure_month = 0
    return periods


def _payment_events(periods: list[dict], customers: list[dict], end_date: date, config: ProjectConfig):
    payments: list[dict] = []
    customer_lookup = {c["customer_id"]: c for c in customers}
    plan_lookup = config.plan_by_id()
    end_limit = pd.Timestamp(end_date, tz="UTC") + timedelta(days=1)

    for period in periods:
        plan = plan_lookup[period["plan_id"]]
        if not plan.is_paid_plan:
            continue
        customer = customer_lookup[period["customer_id"]]
        period_end = period["period_end_timestamp"] if pd.notna(period["period_end_timestamp"]) else end_limit
        cycle = 0
        billing_time = period["period_start_timestamp"]
        while billing_time < min(period_end, end_limit):
            rng = _rng(config, "payment", period["subscription_period_id"], cycle)
            fail_probability = min(0.24, config.payment_failure_rate + customer["_payment_risk"] * 0.38)
            failed = bool(rng.random() < fail_probability)
            initial_type = "initial" if cycle == 0 else "renewal"
            base_key = f"{period['subscription_period_id']}-{billing_time:%Y%m%d}-{cycle}"
            if failed:
                payments.append({
                    "payment_id": f"PAY-{_stable_int(base_key, 1):011d}",
                    "subscription_period_id": period["subscription_period_id"],
                    "customer_id": period["customer_id"],
                    "payment_timestamp": billing_time,
                    "amount": plan.monthly_price,
                    "payment_type": initial_type,
                    "payment_status": "failed",
                    "attempt_number": 1,
                    "ingested_at": billing_time + timedelta(hours=2),
                })
                retry_time = billing_time + timedelta(days=3)
                if retry_time < min(period_end, end_limit):
                    recovered = bool(rng.random() < config.retry_recovery_rate)
                    payments.append({
                        "payment_id": f"PAY-{_stable_int(base_key, 2):011d}",
                        "subscription_period_id": period["subscription_period_id"],
                        "customer_id": period["customer_id"],
                        "payment_timestamp": retry_time,
                        "amount": plan.monthly_price,
                        "payment_type": "retry",
                        "payment_status": "succeeded" if recovered else "failed",
                        "attempt_number": 2,
                        "ingested_at": retry_time + timedelta(hours=2),
                    })
                    succeeded = recovered
                    success_time = retry_time
                else:
                    succeeded = False
                    success_time = billing_time
            else:
                payments.append({
                    "payment_id": f"PAY-{_stable_int(base_key, 1):011d}",
                    "subscription_period_id": period["subscription_period_id"],
                    "customer_id": period["customer_id"],
                    "payment_timestamp": billing_time,
                    "amount": plan.monthly_price,
                    "payment_type": initial_type,
                    "payment_status": "succeeded",
                    "attempt_number": 1,
                    "ingested_at": billing_time + timedelta(hours=2),
                })
                succeeded = True
                success_time = billing_time
            if succeeded and rng.random() < config.refund_rate:
                refund_time = success_time + timedelta(days=int(rng.integers(2, 12)))
                if refund_time < min(period_end, end_limit):
                    payments.append({
                        "payment_id": f"PAY-{_stable_int(base_key, 'refund'):011d}",
                        "subscription_period_id": period["subscription_period_id"],
                        "customer_id": period["customer_id"],
                        "payment_timestamp": refund_time,
                        "amount": -plan.monthly_price,
                        "payment_type": "refund",
                        "payment_status": "succeeded",
                        "attempt_number": 1,
                        "ingested_at": refund_time + timedelta(hours=2),
                    })
            cycle += 1
            billing_time = period["period_start_timestamp"] + pd.DateOffset(months=cycle)
    return payments


def _campaign_daily(assignments: list[dict], outcomes: list[dict], start_date: date, end_date: date, config: ProjectConfig):
    assignment_df = pd.DataFrame(assignments)
    outcome_df = pd.DataFrame(outcomes)
    if assignment_df.empty:
        assignment_groups = {}
        conversion_counts = {}
    else:
        assignment_df = assignment_df.copy()
        assignment_df["assigned_date"] = pd.to_datetime(assignment_df["assigned_at"]).dt.date
        assignment_groups = {
            key: group for key, group in assignment_df.groupby(["campaign_id", "assigned_date"], sort=False)
        }
        outcome_enriched = outcome_df.merge(
            assignment_df[["assignment_id", "first_exposed_at"]], on="assignment_id", how="left"
        )
        outcome_enriched = outcome_enriched[
            outcome_enriched["converted"].astype(bool)
            & outcome_enriched["conversion_timestamp"].notna()
            & outcome_enriched["first_exposed_at"].notna()
            & (outcome_enriched["assignment_arm"] == "Treatment")
        ].copy()
        outcome_enriched["conversion_date"] = pd.to_datetime(outcome_enriched["conversion_timestamp"]).dt.date
        conversion_counts = outcome_enriched.groupby(["campaign_id", "conversion_date"]).size().to_dict()
    rows: list[dict] = []
    for day_ts in pd.date_range(start_date, end_date, freq="D"):
        day = day_ts.date()
        for campaign in config.campaigns:
            rng = _rng(config, "campaign_daily", campaign.campaign_id, day.isoformat())
            daily_assignments = assignment_groups.get((campaign.campaign_id, day), assignment_df.iloc[0:0])
            treatment = daily_assignments[daily_assignments["assignment_arm"] == "Treatment"] if not daily_assignments.empty else daily_assignments
            exposed = (
                treatment["first_exposed_at"].notna().sum()
                if not treatment.empty
                else 0
            )
            attempted_sends = (
                len(treatment)
                if campaign.channel == "Email"
                else 0
            )
            if campaign.channel == "Email":
                impressions = int(exposed)
            else:
                growth, weekday, seasonal = _date_factors(day, start_date, config)
                impressions = max(int(rng.poisson(campaign.mean_daily_eligible * 42 * growth * weekday * seasonal)), int(exposed))
            clicks = int(rng.binomial(impressions, campaign.click_through_rate)) if impressions else 0
            if campaign.channel == "Email":
                production_cost = (
                    campaign.email_production_cost_per_send
                    if attempted_sends > 0
                    else 0.0
                )
                spend = round(
                    production_cost
                    + attempted_sends
                    * campaign.email_cost_per_attempted_send,
                    2,
                )
            else:
                spend = round(
                    clicks
                    * campaign.cost_per_click
                    * float(rng.normal(1.0, 0.06)),
                    2,
                )
            conversions = int(conversion_counts.get((campaign.campaign_id, day), 0))
            platform_conversions = max(0, int(round(conversions * float(rng.normal(1.12, 0.10)))))
            rows.append({
                "metric_date": pd.Timestamp(day),
                "campaign_id": campaign.campaign_id,
                "impressions": impressions,
                "clicks": clicks,
                "spend": spend,
                "platform_attributed_conversions": platform_conversions,
                "ingested_at": pd.Timestamp(day, tz="UTC") + timedelta(days=1, hours=4),
            })
    return rows


def generate_historical_data(
    start_date: date | str | None = None,
    end_date: date | str | None = None,
    config: ProjectConfig = DEFAULT_CONFIG,
) -> SimulationResult:
    """Generate all clean analytical tables for an inclusive date interval."""
    start = pd.Timestamp(start_date or config.history_start_date).date()
    end = pd.Timestamp(end_date or config.historical_cutoff_date).date()
    if end < start:
        raise ValueError("end_date must be on or after start_date")

    dim_plan, dim_campaign = _dimensions(config)
    customers, acquisition_assignments, acquisition_outcomes = _acquisition_population(start, end, config)
    nurture_assignments, nurture_outcomes, nurture_conversion = _nurture_population(customers, start, end, config)
    assignments = acquisition_assignments + nurture_assignments
    outcomes = acquisition_outcomes + nurture_outcomes
    periods = _subscription_history(customers, nurture_conversion, end, config)
    payments = _payment_events(periods, customers, end, config)
    campaign_daily = _campaign_daily(assignments, outcomes, start, end, config)

    customer_public_columns = [
        "customer_id", "prospect_key", "email", "signup_timestamp", "state",
        "experience_level", "initial_acquisition_channel", "first_campaign_id", "source_updated_at",
    ]
    dim_customer = pd.DataFrame(customers)[customer_public_columns].sort_values("customer_id").reset_index(drop=True)
    dim_customer["first_campaign_id"] = dim_customer["first_campaign_id"].astype("Int64")
    fact_campaign_assignment = pd.DataFrame(assignments).sort_values(["assigned_at", "assignment_id"]).reset_index(drop=True)
    fact_campaign_assignment["customer_id"] = fact_campaign_assignment["customer_id"].astype("Int64")
    clean_tables = {
        "dim_plan": dim_plan,
        "dim_campaign": dim_campaign,
        "dim_customer": dim_customer,
        "fact_subscription_period": pd.DataFrame(periods).sort_values(["customer_id", "period_start_timestamp"]).reset_index(drop=True),
        "fact_payment": pd.DataFrame(payments).sort_values(["payment_timestamp", "payment_id"]).reset_index(drop=True),
        "fact_campaign_daily": pd.DataFrame(campaign_daily).sort_values(["metric_date", "campaign_id"]).reset_index(drop=True),
        "fact_campaign_assignment": fact_campaign_assignment,
    }
    truth_tables = {
        "campaign_outcome_truth": pd.DataFrame(outcomes).sort_values("assignment_id").reset_index(drop=True),
    }
    return SimulationResult(clean_tables, truth_tables, start, end)


if __name__ == "__main__":
    result = generate_historical_data()
    for table_name in TABLE_ORDER:
        print(f"{table_name}: {len(result.clean_tables[table_name]):,} rows")
