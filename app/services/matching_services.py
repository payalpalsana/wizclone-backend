# app/services/matching_service.py
# ─────────────────────────────────────────────────────────────
# C-04 — AI Smart Matching Engine
#
# Matches a new item name against all workspace template names.
#
# Current engine : difflib fuzzy matching (free, zero latency)
# Future engine  : monday.com Models API (plug in when approved)
#                  Replace _models_api_match() body only —
#                  everything else stays the same.
#
# Match result shape (always returned):
# {
#     "matched_name"  : str | None,   # template name that best matches
#     "matched_id"    : str | None,   # template UUID from DB
#     "confidence"    : float,        # 0.0 – 100.0
#     "method"        : str,          # "EXACT_MATCH" | "AI"
#     "ai_used"       : bool,         # True only when Models API actually ran
# }
# ─────────────────────────────────────────────────────────────

import difflib


# ══════════════════════════════════════════════════════════════
# Sensitivity thresholds
# ══════════════════════════════════════════════════════════════
# Defined in scope C-04.
# Worker reads workspace sensitivity from workspace_settings
# and passes it here as a string.

SENSITIVITY_THRESHOLDS: dict[str, float] = {
    "STRICT":   90.0,
    "BALANCED": 75.0,
    "LOOSE":    55.0,
}

DEFAULT_SENSITIVITY = "BALANCED"


def get_threshold(sensitivity: str) -> float:
    """
    Return the confidence threshold for a given sensitivity level.
    Falls back to BALANCED if sensitivity string is unrecognised.
    """
    return SENSITIVITY_THRESHOLDS.get(sensitivity.upper(), 75.0)


# ══════════════════════════════════════════════════════════════
# FUTURE SLOT — monday.com Models API
# ══════════════════════════════════════════════════════════════
#
# CURRENT STATUS: Commented out — waiting for monday.com approval
#
# WHY IS THIS COMMENTED OUT?
#   monday.com Models API is in "early preview" stage.
#   It requires filling a form and getting manual approval from monday.com.
#   Until approved, the API returns 401 Unauthorized for all requests.
#   Even if we uncomment this code today, it will not work without approval.
#   We confirmed this from monday.com own AI documentation answer.
#
# WHAT DOES THIS CODE DO WHEN ACTIVE?
#   Instead of difflib character matching, it sends the item name
#   and all template names to monday.com AI model.
#   The AI understands INTENT — so "Acme setup" can match
#   "New Client Onboarding" even though the words are completely different.
#   This is smarter than difflib which only matches similar characters.
#
# HOW TO ACTIVATE WHEN MONDAY.COM APPROVES ACCESS:
#   Step 1: Add to .env file:
#           MONDAY_MODELS_API_URL=https://api.monday.com/platform-ai-gateway/openai/v1
#   Step 2: Add to app/core/config.py Settings class:
#           monday_models_api_url: str
#   Step 3: Set USE_MODELS_API = True on the line below
#   Step 4: Uncomment the code block inside _models_api_match() below
#   Step 5: Remove the "return None" line at the bottom of that function
#   Step 6: Add "import httpx" at the top of this file
#   Nothing else changes — worker.py, webhook.py all stay the same.
#
# AI CREDITS:
#   Credits are deducted from the USER monday.com account, not ours.
#   PRO and BUSINESS plan users get AI matching (C-10 enforcement).
#
# APPLY FOR ACCESS:
#   https://developer.monday.com/api-reference/docs/models-api

USE_AI_MATCHING = True


# async def _models_api_match(
#     item_name:      str,
#     template_names: list[str],
#     access_token:   str,
# ) -> dict | None:
#     """
#     WHY THIS FUNCTION EXISTS:
#         AI matching engine slot for monday.com Models API.
#         When active, replaces difflib with real AI semantic understanding.
#         Currently returns None so caller automatically uses difflib instead.
#
#     HOW IT WORKS WHEN ACTIVE:
#         1. Builds a prompt listing all template names
#         2. Asks AI: which template does this new item name belong to?
#         3. AI returns best matching template name + confidence score 0-100
#         4. We validate the name exists in our list and return the result
#
#     WHAT HAPPENS IF IT FAILS:
#         Returns None — match_item_to_template() catches this and
#         automatically falls back to difflib. Core functionality never breaks.
#
#     WHEN TO UNCOMMENT:
#         When monday.com approves Models API access.
#         Follow the 6 steps listed above in the comments.
#     """
#
#     # ════════════════════════════════════════════════════════
#     # STEP 4: UNCOMMENT THIS ENTIRE BLOCK WHEN MODELS API IS APPROVED
#     # ════════════════════════════════════════════════════════
#     #
#     # from app.core.config import settings
#     # import httpx
#     #
#     # # Build candidate list for the prompt
#     # candidates = "\n".join(f"- {name}" for name in template_names)
#     #
#     # # Prompt instructs AI to match by intent not just exact words
#     # prompt = (
#     #     f"You are a template matcher for a task management tool.\n"
#     #     f"Given the new item name, pick the BEST matching template "
#     #     f"from the list. Consider intent and meaning, not just exact words.\n\n"
#     #     f"New item name: {item_name}\n\n"
#     #     f"Available templates:\n{candidates}\n\n"
#     #     f"Reply with ONLY this format (nothing else):\n"
#     #     f"<template name> | <confidence 0-100>\n\n"
#     #     f"Example: New Client Onboarding | 87"
#     # )
#     #
#     # try:
#     #     async with httpx.AsyncClient(timeout=10) as client:
#     #         response = await client.post(
#     #             f"{settings.monday_models_api_url}/chat/completions",
#     #             headers={
#     #                 "Authorization": f"Bearer {access_token}",
#     #                 "Content-Type":  "application/json",
#     #             },
#     #             json={
#     #                 "model":      "monday-standard",
#     #                 "messages":   [{"role": "user", "content": prompt}],
#     #                 "max_tokens": 50,
#     #             },
#     #         )
#     #
#     #     if response.status_code != 200:
#     #         print(f"[matching] Models API {response.status_code} — using difflib")
#     #         return None
#     #
#     #     # Parse response — expected: "Template Name | 87"
#     #     text  = response.json()["choices"][0]["message"]["content"].strip()
#     #     parts = text.split("|")
#     #
#     #     if len(parts) != 2:
#     #         print(f"[matching] Models API bad format: {text} — using difflib")
#     #         return None
#     #
#     #     matched_name = parts[0].strip()
#     #     confidence   = float(parts[1].strip())
#     #
#     #     # Safety: AI must return a name that exists in our template list
#     #     # Prevents hallucinated template names from being accepted
#     #     if matched_name not in template_names:
#     #         print(f"[matching] Models API returned unknown template '{matched_name}' — using difflib")
#     #         return None
#     #
#     #     print(f"[matching] Models API: '{matched_name}' at {confidence}%")
#     #
#     #     return {
#     #         "matched_name": matched_name,
#     #         "confidence":   confidence,
#     #         "method":       "AI",    # saved to automation_events.match_method
#     #         "ai_used":      True,    # ai_fallback_used = False in DB
#     #     }
#     #
#     # except Exception as e:
#     #     print(f"[matching] Models API error: {e} — using difflib")
#     #     return None
#     #
#     # ════════════════════════════════════════════════════════
#     # END OF BLOCK — STEP 4
#     # ════════════════════════════════════════════════════════
#
#     # STEP 5: DELETE THIS LINE when you uncomment the block above
#     return None


async def _ai_semantic_match(
    item_name:      str,
    template_names: list[str],
) -> dict | None:
    """
    Groq AI Matching Engine
    """

    # ════════════════════════════════════════════════════════
    from app.core.config import settings
    import httpx

    if not settings.groq_api_key:
        print("[matching] No GROQ_API_KEY found — falling back to difflib")
        return None

    # Build candidate list for the prompt
    candidates = "\n".join(f"- {name}" for name in template_names)

    # Prompt instructs AI to match by intent not just exact words
    prompt = (
        f"You are a template matcher for a task management tool.\n"
        f"Given the new item name, pick the BEST matching template "
        f"from the list. Consider intent and meaning, not just exact words.\n\n"
        f"New item name: {item_name}\n\n"
        f"Available templates:\n{candidates}\n\n"
        f"Reply with ONLY this format (nothing else):\n"
        f"<template name> | <confidence 0-100>\n\n"
        f"Example: New Client Onboarding | 87"
    )

    try:
        async with httpx.AsyncClient(timeout=10) as client:
            response = await client.post(
                "https://api.groq.com/openai/v1/chat/completions",
                headers={
                    "Authorization": f"Bearer {settings.groq_api_key}",
                    "Content-Type":  "application/json",
                },
                json={
                    "model":      "llama-3.1-8b-instant",
                    "messages":   [{"role": "user", "content": prompt}],
                    "max_tokens": 50,
                },
            )

        if response.status_code != 200:
            print(f"[matching] Groq API {response.status_code} — using difflib")
            return None

        # Parse response — expected: "Template Name | 87"
        text  = response.json()["choices"][0]["message"]["content"].strip()
        parts = text.split("|")

        if len(parts) != 2:
            print(f"[matching] Groq API bad format: {text} — using difflib")
            return None

        matched_name = parts[0].strip()
        confidence   = float(parts[1].strip())

        # Safety: AI must return a name that exists in our template list
        # Prevents hallucinated template names from being accepted
        if matched_name not in template_names:
            print(f"[matching] Groq API returned unknown template '{matched_name}' — using difflib")
            return None

        print(f"[matching] Groq API: '{matched_name}' at {confidence}%")

        return {
            "matched_name": matched_name,
            "confidence":   confidence,
            "method":       "AI",    # saved to automation_events.match_method
            "ai_used":      True,    # ai_fallback_used = False in DB
        }

    except Exception as e:
        print(f"[matching] Groq API error: {e} — using difflib")
        return None

async def generate_template_from_ai(item_name: str) -> dict | None:
    """
    Pure AI Generator for the frontend.
    Generates a template name and subitems based on the item name without looking at DB templates.
    """
    from app.core.config import settings
    import httpx

    if not settings.groq_api_key:
        return None

    prompt = (
        f"You are an intelligent task management assistant.\n"
        f"Given a user prompt/item name, generate a categorized 'Template Name' "
        f"that describes this type of work.\n"
        f"ALSO, dynamically generate 3 to 5 specific subtasks to complete this item.\n\n"
        f"User Prompt: {item_name}\n\n"
        f"Reply with ONLY this format (nothing else):\n"
        f"<Generated Template Name>\n"
        f"- <subtask 1>\n"
        f"- <subtask 2>\n"
        f"- <subtask 3>\n\n"
        f"Example:\n"
        f"Marketing Campaign\n"
        f"- Conduct market research\n"
        f"- Draft ad copy\n"
        f"- Launch ads"
    )

    try:
        async with httpx.AsyncClient(timeout=10) as client:
            response = await client.post(
                "https://api.groq.com/openai/v1/chat/completions",
                headers={
                    "Authorization": f"Bearer {settings.groq_api_key}",
                    "Content-Type":  "application/json",
                },
                json={
                    "model":      "llama-3.1-8b-instant",
                    "messages":   [{"role": "user", "content": prompt}],
                    "max_tokens": 150,
                },
            )

        if response.status_code != 200:
            return None

        text  = response.json()["choices"][0]["message"]["content"].strip()
        lines = [line.strip() for line in text.split("\n") if line.strip()]

        if not lines:
            return None

        # First line is Template Name
        suggested_name = lines[0].strip()
        
        # Following lines with '-' are subitems
        ai_subitems = []
        for line in lines[1:]:
            if line.startswith("-"):
                ai_subitems.append(line.lstrip("- ").strip())

        return {
            "suggested_item_name": suggested_name,
            "ai_suggested_subitems": ai_subitems,
        }

    except Exception:
        return None


# ══════════════════════════════════════════════════════════════
# PRIMARY ENGINE — difflib fuzzy matching
# ══════════════════════════════════════════════════════════════

def _fuzzy_match(
    item_name:      str,
    template_names: list[str],
) -> dict:
    """
    Match item_name against template_names using Python difflib.

    Scoring:
      Base  : difflib SequenceMatcher ratio × 100
      Boost : 85.0 if template name is a substring of item name
              80.0 if item name is a substring of template name
      Boost only applies if the shorter string is at least 4 chars
      (prevents "QA" matching everything that contains "qa")

    Returns best match with confidence score.
    Returns matched_name=None and confidence=0.0 if no templates given.
    """
    if not template_names:
        return {
            "matched_name": None,
            "confidence":   0.0,
            "method":       "EXACT_MATCH",
            "ai_used":      False,
        }

    item_lower = item_name.lower().strip()
    best_name  = None
    best_score = 0.0

    for name in template_names:
        name_lower = name.lower().strip()

        if not name_lower:
            continue

        # ── Base similarity score ──
        score = difflib.SequenceMatcher(
            None, item_lower, name_lower
        ).ratio() * 100.0

        # ── Substring boosts ──
        # Only boost if the matching substring is meaningful (≥ 4 chars)
        # Prevents short template names like "QA" getting 85% on everything

        # Template name found inside item name
        # e.g. item="New Client Onboarding — Acme Corp", template="New Client Onboarding"
        if len(name_lower) >= 4 and name_lower in item_lower:
            score = max(score, 85.0)

        # Item name found inside template name
        # e.g. item="Onboarding", template="New Client Onboarding"
        if len(item_lower) >= 4 and item_lower in name_lower:
            score = max(score, 80.0)

        if score > best_score:
            best_score = score
            best_name  = name

    return {
        "matched_name": best_name,
        "confidence":   round(best_score, 2),
        "method":       "EXACT_MATCH",
        "ai_used":      False,
    }


# ══════════════════════════════════════════════════════════════
# PUBLIC FUNCTION — called by worker.py
# ══════════════════════════════════════════════════════════════

async def match_item_to_template(
    item_name:      str,
    templates:      list[dict],   # [{"id": uuid, "name": str}, ...]
    access_token:   str,
    sensitivity:    str = DEFAULT_SENSITIVITY,
) -> dict:
    """
    Main entry point for C-04 matching.

    Called by worker.py with:
      - item_name    : name of the newly created monday.com item
      - templates    : list of {id, name} dicts from templates table
      - access_token : workspace OAuth token (used by Models API when ready)
      - sensitivity  : "STRICT" | "BALANCED" | "LOOSE"

    Returns:
    {
        "matched_id"   : str | None,   # template UUID — None if no match
        "matched_name" : str | None,   # template name — None if no match
        "confidence"   : float,        # 0.0 – 100.0
        "method"       : str,          # "EXACT_MATCH" | "AI"
        "ai_used"      : bool,
        "above_threshold" : bool,      # True = trigger subitem copy
        "threshold"    : float,        # threshold that was applied
    }
    """

    # ── Guard: empty item name ──
    if not item_name or not item_name.strip():
        return _no_match_result(sensitivity, reason="empty item name")

    # ── Guard: no templates in workspace ──
    if not templates:
        return _no_match_result(sensitivity, reason="no templates")

    template_names = [t["name"] for t in templates]

    # ── Try Groq AI first ──
    result = None
    if USE_AI_MATCHING:
        try:
            result = await _ai_semantic_match(item_name, template_names)
        except Exception:
            result = None   # fall through to fuzzy

    # ── Fall back to fuzzy matching ──
    if result is None:
        result = _fuzzy_match(item_name, template_names)

    # ── Find the matched template UUID from DB list ──
    matched_id = None
    if result["matched_name"]:
        matched = next(
            (t for t in templates if t["name"] == result["matched_name"]),
            None,
        )
        matched_id = matched["id"] if matched else None

    # ── Apply sensitivity threshold ──
    threshold       = get_threshold(sensitivity)
    above_threshold = (
        result["matched_name"] is not None and
        result["confidence"] >= threshold
    )

    return {
        "matched_id":       matched_id,
        "matched_name":     result["matched_name"],
        "confidence":       result["confidence"],
        "method":           result["method"],
        "ai_used":          result["ai_used"],
        "above_threshold":  above_threshold,
        "threshold":        threshold,
    }


# ══════════════════════════════════════════════════════════════
# Helper
# ══════════════════════════════════════════════════════════════

def _no_match_result(sensitivity: str, reason: str = "") -> dict:
    """Return a clean no-match result dict."""
    return {
        "matched_id":       None,
        "matched_name":     None,
        "confidence":       0.0,
        "method":           "EXACT_MATCH",
        "ai_used":          False,
        "above_threshold":  False,
        "threshold":        get_threshold(sensitivity),
    }