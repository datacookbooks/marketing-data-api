"""Central configuration for the synthetic guitar-lesson business.

All commercial assumptions live here so the generators never scatter prices,
costs, experiment shares, or defect rates through their implementation.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Mapping, Tuple


@dataclass(frozen=True)
class PlanConfig:
    plan_id: int
    plan_name: str
    monthly_price: float
    estimated_monthly_variable_cost: float
    weekly_recorded_lesson_limit: int | None
    private_sessions_per_month: int
    is_paid_plan: bool


@dataclass(frozen=True)
class CampaignConfig:
    campaign_id: int
    campaign_name: str
    channel: str
    objective: str
    active_start_date: date
    active_end_date: date | None
    treatment_share: float
    mean_daily_eligible: float
    baseline_conversion_rate: float
    intention_to_treat_lift: float
    exposure_rate: float
    click_through_rate: float
    cost_per_click: float


@dataclass(frozen=True)
class DefectConfig:
    duplicate_delivery_rate: float = 0.008
    exact_duplicate_rate: float = 0.003
    blank_email_rate: float = 0.012
    missing_click_rate: float = 0.008
    malformed_numeric_rate: float = 0.004
    late_correction_rate: float = 0.018
    unknown_campaign_rate: float = 0.003
    inconsistent_text_rate: float = 0.025


@dataclass(frozen=True)
class ProjectConfig:
    history_start_date: date = date(2024, 1, 1)
    historical_cutoff_date: date = date(2025, 12, 31)
    random_seed: int = 20260818
    base_daily_organic_signups: float = 4.5
    annual_growth_rate: float = 0.16
    payment_failure_rate: float = 0.065
    retry_recovery_rate: float = 0.72
    refund_rate: float = 0.018
    plans: Tuple[PlanConfig, ...] = field(default_factory=lambda: (
        PlanConfig(1, "Free", 0.0, 1.50, 2, 0, False),
        PlanConfig(2, "Pro", 24.0, 4.0, None, 0, True),
        PlanConfig(3, "Master", 119.0, 72.0, None, 4, True),
    ))
    campaigns: Tuple[CampaignConfig, ...] = field(default_factory=lambda: (
        CampaignConfig(
            101, "Guitar Lesson Search", "Paid Search", "Acquisition",
            date(2024, 1, 1), None, 0.85, 25.0, 0.090, 0.018, 0.90, 0.075, 2.10,
        ),
        CampaignConfig(
            102, "Learn Guitar Social", "Paid Social", "Acquisition",
            date(2024, 1, 1), None, 0.85, 38.0, 0.035, 0.010, 0.82, 0.020, 1.35,
        ),
        CampaignConfig(
            103, "Free-to-Pro Nurture", "Email", "Free-to-paid conversion",
            date(2024, 1, 1), None, 0.85, 0.0, 0.045, 0.032, 0.76, 0.115, 0.08,
        ),
    ))
    defects: DefectConfig = field(default_factory=DefectConfig)
    state_weights: Mapping[str, float] = field(default_factory=lambda: {
        "CA": 0.18, "TX": 0.13, "FL": 0.10, "NY": 0.09, "PA": 0.07,
        "IL": 0.06, "OH": 0.06, "GA": 0.06, "NC": 0.06, "MI": 0.05,
        "VA": 0.05, "WA": 0.05, "CO": 0.04,
    })

    def plan_by_id(self) -> dict[int, PlanConfig]:
        return {plan.plan_id: plan for plan in self.plans}

    def campaign_by_id(self) -> dict[int, CampaignConfig]:
        return {campaign.campaign_id: campaign for campaign in self.campaigns}


DEFAULT_CONFIG = ProjectConfig()
