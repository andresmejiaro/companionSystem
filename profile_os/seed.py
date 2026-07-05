"""Seed the two example Assistant Profiles: sidra and tara."""

from . import prompts
from .storage import Store

SIDRA_BASE = prompts.load("sidra_base")
SIDRA_ROLE = prompts.load("sidra_role")
TARA_BASE = prompts.load("tara_base")
TARA_ROLE = prompts.load("tara_role")
SECRETARY_BASE = prompts.load("secretary_base")
SECRETARY_ROLE = prompts.load("secretary_role")


def seed(store: Store) -> None:
    existing = {p["id"] for p in store.list_profiles()}

    if "sidra" not in existing:
        store.create_profile(
            "sidra", "Sidra", SIDRA_BASE, SIDRA_ROLE,
            description="Agentic coding: task contracts, diff review, failure scars, model routing.",
            allowed_tools=["read_file", "write_file", "run_tests", "git_diff"],
            memory_policy={"kinds": ["decision", "failure_scar", "note"], "max_boot_events": 10},
            closeout_rules="Summarize task outcome, open risks, and any new failure scars.",
            initial_state="No active task contract.",
        )
        store.add_domain_record("sidra", "task_contracts", {
            "task": "example: add pagination to /items",
            "allowed_context": ["api/items.py", "tests/test_items.py"],
            "acceptance": "tests pass; no schema change", "status": "done"})
        store.add_domain_record("sidra", "model_routing", {
            "task_type": "mechanical rename", "route_to": "cheap model",
            "notes": "no reasoning needed"})
        store.remember("sidra", {"kind": "failure_scar",
                                 "content": "Deleted a migration file assuming it was unused; always check migration chains first.",
                                 "tags": ["migrations", "deletion"]})

    if "tara" not in existing:
        store.create_profile(
            "tara", "Tara", TARA_BASE, TARA_ROLE,
            description="Food tracking: nutrition facts, eaten food, product calibration, meal history.",
            allowed_tools=["log_food", "query_products", "daily_summary"],
            memory_policy={"kinds": ["fact", "observation", "preference", "note"], "max_boot_events": 10},
            closeout_rules="Write daily intake summary and flag uncalibrated products.",
            initial_state="No meals logged today.",
        )
        store.add_domain_record("tara", "products", {
            "name": "Greek yogurt 500g", "brand": "ExampleDairy",
            "per_100g": {"kcal": 97, "protein_g": 9, "carbs_g": 4, "fat_g": 5},
            "calibrated": True})
        store.add_domain_record("tara", "products", {
            "name": "Granola house mix", "brand": None,
            "per_100g": {"kcal": 450, "protein_g": 10, "carbs_g": 60, "fat_g": 18},
            "calibrated": False})
        store.add_domain_record("tara", "meals", {
            "food": "Greek yogurt with granola", "grams": 250,
            "when": "2026-07-03T08:30:00", "kcal_est": 420})
        store.remember("tara", {"kind": "fact",
                                "content": "User prefers protein-forward breakfasts.",
                                "tags": ["preference", "breakfast"]})

    if "secretary" not in existing:
        # Prompt-only companion: no domain records, no stores, no schemas.
        # Any structure (e.g. a todo store) must be proposed by the
        # companion itself, through tools, when the need shows itself.
        store.create_profile(
            "secretary", "Secretary", SECRETARY_BASE, SECRETARY_ROLE,
            description="Commitments, tasks, follow-ups; grows its own structure via store proposals.",
            allowed_tools=[],
            memory_policy={"kinds": ["decision", "fact", "note",
                                     "observation", "preference"],
                           "max_boot_events": 10},
            closeout_rules="Write a handoff note: open commitments, pending store proposals, next checks.",
            initial_state="Fresh profile. No commitments recorded, no stores.",
        )
