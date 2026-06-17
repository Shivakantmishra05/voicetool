"""
real_estate_agent.py — DreamHome Properties
Riya Sharma — Outgoing call agent, v3
"""

SYSTEM_PROMPT = """
Tu Riya Sharma hai, DreamHome Properties ki senior property consultant.
Tu outbound phone call pe hai. Caller ne property enquiry ki thi.

Bolne ka style:
- Natural Hinglish. Caller Hindi/English switch kare to usi language mein raho.
- Feminine grammar only: "bol rahi hoon", "samajh gayi", "bhej deti hoon".
- Pehle short reaction, phir jawab: "Achha.", "Samajh gayi.", "Theek hai."
- 1 sentence preferred. Max 15 words. Max 1 question.
- Never explain, summarize, list multiple options, or sound corporate.
- Never say: certainly, absolutely, noted, great question, how may I help, AI, bot.

Flow:
1. Confirm identity.
2. Intro: DreamHome se hoon, enquiry ke baare mein call tha.
3. Ask one missing fact at a time: location → BHK → purpose → budget.
4. If enough facts are known, use matched project context and offer WhatsApp/site visit.

Inventory safety:
- Only these projects exist: Green Valley, Orchid Heights, Lotus Residency, The Greens, Skyline Heights.
- Never invent project, price, facility, RERA, floor, discount, availability, or visit slot.
- Not available: 4BHK, villa, plot, studio, PG, rental, commercial, unknown society/sector.
- Unknown area/project exact reply:
"Woh area hamare paas covered nahi hai abhi. Hamare projects Greater Noida West mein hain — agar kabhi us side consider karo toh batana."

Objections:
- Price high: "Samajh gayi. Site pe actual offer better samajh aata hai."
- Thinking: "Haan, fair hai. Details WhatsApp pe bhej deti hoon?"
- Family approval: "Bilkul. Weekend pe family ke saath visit easy rahega."
- Busy: "Theek hai sir, kab call karun?"

AI question exact reply:
"Nahi sir, main Riya hoon DreamHome se."
"""


# ── Greetings ──────────────────────────────────────────────

OUTGOING_CONFIRM_LINE = "Namaste sir... kya main {customer_name} ji se baat kar rahi hoon?"

OUTGOING_INTRO_LINE = (
    "Main Riya, DreamHome Properties se. "
    "Aapki property enquiry aayi thi... abhi ek minute baat ho paayegi?"
)

INCOMING_GREETING = "Haan ji, DreamHome Properties. Bataiye?"

GREETING = OUTGOING_CONFIRM_LINE


# ── Escalation ─────────────────────────────────────────────

ESCALATION_TRIGGERS = {
    "not_interested_repeat": "Client ne 2 baar not interested bola — exit kiya.",
    "abusive":               "Client aggressive/abusive tha — graceful exit.",
    "unavailable_inventory": "Client ne unavailable inventory maangi — redirected, exited.",
    "out_of_scope_question": "Legal/RERA/technical question — senior team refer kiya.",
}


def get_escalation_flag(trigger_key: str) -> dict:
    return {
        "escalation_triggered": True,
        "escalation_reason": ESCALATION_TRIGGERS.get(trigger_key, "unknown"),
        "escalation_key": trigger_key,
        "requires_human_followup": True,
    }


# ── Summary Prompt ─────────────────────────────────────────

SUMMARY_PROMPT = """
Extract a final CRM summary from this real estate sale inquiry call.
Return strict JSON only — no markdown, no backticks, no explanation.

Required keys: summary, lead_status, sentiment, outcome, lead_info, crm_enrichment.

lead_status: new | qualified | visit_booked | callback_scheduled | not_interested | needs_follow_up | escalated

lead_info keys:
  name, project_interest, property_type, budget, possession_timeline,
  purpose, whatsapp_confirmed, visit_interest, objections.

Field guidance:
- project_interest: Green Valley / Orchid Heights / Lotus Residency / The Greens / Skyline Heights / unknown
- property_type: 1 BHK / 2 BHK / 3 BHK / unavailable / unknown
- budget: stated buyer budget, null if not mentioned
- possession_timeline: what buyer prefers, or callback time if busy
- purpose: self-use / investment / broker / tenant / unknown
- whatsapp_confirmed: yes / no / unknown
- visit_interest: site visit / callback / not interested / unknown
- objections: array — price / location / possession / trust / availability / family_approval / callback_requested / out_of_scope / other

Rules:
- null for anything not clearly stated. Never invent.
- Busy caller → lead_status = callback_scheduled
- Escalation → lead_status = escalated, note reason in objections

crm_enrichment keys:
  lead_score, language, intent_type, conversation_stage, customer_profile,
  visit_day, visit_time, decision_maker, call_type, escalation_triggered, escalation_reason.
"""


# ── Context Builder ────────────────────────────────────────

def build_dynamic_response_context(
    persona_context: str,
    memory_context: str,
    stage_context: str = "",
    profile_context: str = "",
    objection_context: str = "",
    lead_score_context: str = "",
    language_context: str = "",
    matched_project_context: str = "",
) -> str:
    sections = []

    if language_context:
        sections.append(language_context)
    if memory_context and "No confirmed facts yet" not in memory_context:
        sections.append(memory_context)
    if matched_project_context:
        sections.append(matched_project_context)
    if stage_context:
        sections.append(stage_context)
    if objection_context and "None detected" not in objection_context:
        sections.append(objection_context)

    sections.append(
        "This turn only:\n"
        "- One sentence. Max 15 words. Max one question.\n"
        "- Use known facts; never ask known/refused fields.\n"
        "- Never say facts not in memory, inventory, or matched project.\n"
        "- Prefer direct reply. Never explain or summarize.\n"
        "- Do not give multiple options unless caller asks."
    )

    return "\n\n".join(s for s in sections if s)
