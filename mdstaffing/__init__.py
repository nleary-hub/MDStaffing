"""MDStaffing — rule-driven physician scheduling for a cardiovascular and
pulmonary service line."""
from .config import load_service_line
from .fairness import Ledger
from .solver import Scheduler, SolveResult, solve

__all__ = ["load_service_line", "Ledger", "Scheduler", "SolveResult", "solve"]
__version__ = "0.1.0"
