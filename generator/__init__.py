"""Synthetic data generator for the guitar-lesson marketing analytics project."""

from .config import DEFAULT_CONFIG, ProjectConfig
from .historical_seed import SimulationResult, generate_historical_data

__all__ = [
    "DEFAULT_CONFIG",
    "ProjectConfig",
    "SimulationResult",
    "generate_historical_data",
]
