from .client import LLMClient, get_client, UNAVAILABLE
from .planner import Planner, get_planner, estate_summary
# Skill mechanism (idc/llm/skills.py + runner.py). The four wired skills
# (seven_r / audit_match / assess_wave / review_plan) ship a SKILL.md file as
# their single source of truth — no duplicate Python constant, no drift guard.
# The three loop-driven skills (lz_design / network_policy / waveplan) are NOT
# yet skill-wired: their prompts are Python constants used directly by their
# methods, with no shipped file, so they register a _FALLBACK here so the
# Skills UI can list + edit them (editing creates an overlay file, but the
# method still reads the constant until the skill-wiring refactor lands).
# See idc/llm/skills.py for the format.
from .client import LZ_DESIGN_SYSTEM, NETWORK_POLICY_SYSTEM
from .planner import SYSTEM as WAVEPLAN_SYSTEM
from .skills import register_fallback

register_fallback("lz_design", LZ_DESIGN_SYSTEM)
register_fallback("network_policy", NETWORK_POLICY_SYSTEM)
register_fallback("waveplan", WAVEPLAN_SYSTEM)

__all__ = ["LLMClient", "get_client", "UNAVAILABLE", "Planner", "get_planner",
           "estate_summary", "run_skill", "get_skill", "register_fallback",
           "register_validator", "Skill"]

# convenient re-exports for call sites
from .runner import run_skill  # noqa: E402
from .skills import Skill, get_skill  # noqa: E402
from .runner import register_validator  # noqa: E402