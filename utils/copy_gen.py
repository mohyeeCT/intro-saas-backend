import re
import json


# ── Sanitiser ─────────────────────────────────────────────────────────────────

def sanitise(text: str, brand_name: str = "") -> str:
    """Strip em dashes, fix brand casing, remove surrounding quotes."""
    if not text:
        return ""
    text = text.replace("\u2014", " ").replace("\u2013", " ")
    text = text.strip().strip('"').strip("'").strip()
    if brand_name:
        text = re.sub(re.escape(brand_name), brand_name, text, flags=re.IGNORECASE)
    return text


# ── Business type context ─────────────────────────────────────────────────────

_BIZ_CONTEXT = {
    "b2b": (
        "B2B audience. Prioritise clarity on process, capability, and ROI. "
        "No consumer CTAs. Tone is professional and direct."
    ),
    "b2c": (
        "B2C audience. Conversational and benefit-led. A light CTA or value hook "
        "fits naturally at the end."
    ),
    "ecommerce": (
        "Ecommerce page. Lead with product benefit or category value. Address "
        "buyer intent directly. Keep it scannable."
    ),
    "service": (
        "Service page. Build trust through clarity on what is offered and for whom. "
        "Emphasise expertise and outcomes."
    ),
    "local": (
        "Local business page. Reference service area or local context where it adds "
        "value. Tone is approachable and direct."
    ),
    "general": "General audience. Write clearly and helpfully without assumed knowledge.",
}


# ── Page template rules ───────────────────────────────────────────────────────

_TEMPLATE_RULES = {
    "category": (
        "This is an ecommerce category page. The intro should orient the visitor to "
        "the product range and its key benefits or use cases. Represent the primary keyword "
        "naturally in the opening paragraph. Do not describe a single product — describe "
        "the category. No CTA."
    ),
    "product": (
        "This is a product page. Lead with what the product does and who it is for. "
        "Represent the primary keyword naturally in the opening paragraph. One supporting keyword "
        "should appear in a subsequent sentence. Keep it specific to this product."
    ),
    "service_lp": (
        "This is a service or landing page. The intro should establish what the service "
        "does, who it is for, and why it matters. Represent the primary keyword naturally "
        "in the opening paragraph. "
        "Supporting keywords woven naturally into subsequent sentences. "
        "Avoid generic opener phrases."
    ),
    "location": (
        "This is a location page. The intro should reference the location and the service "
        "offered there. Represent the primary keyword, likely a location-modified phrase, "
        "naturally in the opening paragraph. Keep it grounded in local context."
    ),
    "blog": (
        "This is a blog or editorial page. The intro should draw the reader in with a "
        "clear statement of what they will learn or gain. Represent the primary keyword "
        "naturally in the opening paragraph. Engagement matters here more than on other templates. "
        "No hard sell."
    ),
    "brand": (
        "This is a brand or about page. The intro should communicate who the brand is "
        "and what it stands for. Primary keyword used naturally. Tone should reflect "
        "brand voice closely. Avoid corporate boilerplate."
    ),
}


# ── Provider routing ──────────────────────────────────────────────────────────

UNSUPPORTED_CLAIM_GUARDRAIL = """
UNSUPPORTED CLAIM GUARDRAIL:
- Do not state or imply return, shipping, delivery, warranty, guarantee, refund,
  exchange, eligibility, availability, stock, pricing, discount, certification,
  compliance, safety, legal, medical, or performance claims unless they are
  explicitly present in approved brand guidance or scraped page content.
- Treat GSC, DataForSEO, keywords, templates, business type, and niche context as
  strategy signals only. They are not proof for factual claims.
- If a risky claim would normally be expected for this page type, write a safer
  general benefit, product/category fit, audience, use case, or topic framing
  instead.
""".strip()


SCRAPED_CONTEXT_GUARDRAIL = """
SCRAPED CONTEXT GUARDRAIL:
- Use scraped page content only to understand the page topic and stable details.
- Do not turn scraped prices, exact sizes, stock levels, product counts, variant
  counts, discounts, or policy details into claims unless the brand guidance
  clearly approves using those specifics.
- Prefer stable, general wording when context appears temporary or inventory-led.
""".strip()


DEFAULT_MODELS = {
    "Claude": "claude-sonnet-5",
    "OpenAI": "gpt-5.5",
    "Gemini (free)": "gemini-2.0-flash",
    "Mistral (free tier)": "mistral-small-latest",
    "Groq (free tier)": "llama3-70b-8192",
}


def _extract_anthropic_text(content) -> str:
    """Return concatenated text blocks from an Anthropic Messages response."""
    text = "\n".join(
        str(block.text)
        for block in (content or [])
        if getattr(block, "type", "text") == "text" and getattr(block, "text", None)
    ).strip()
    if not text:
        raise RuntimeError("AI provider returned an empty text response")
    return text


def _anthropic_request_options(model: str, max_tokens: int) -> dict:
    options = {"model": model, "max_tokens": max_tokens}
    if (model or "").startswith("claude-sonnet-5"):
        options["thinking"] = {"type": "disabled"}
    return options


def _call_claude(api_key: str, prompt: str, max_tokens: int = 1000, model: str = None) -> str:
    import anthropic
    client = anthropic.Anthropic(api_key=api_key)
    resolved_model = model or DEFAULT_MODELS["Claude"]
    msg = client.messages.create(
        **_anthropic_request_options(resolved_model, max_tokens),
        messages=[{"role": "user", "content": prompt}],
    )
    return _extract_anthropic_text(msg.content)


def _call_openai(api_key: str, prompt: str, max_tokens: int = 1000, model: str = None) -> str:
    from openai import OpenAI
    client = OpenAI(api_key=api_key)
    resolved_model = model or DEFAULT_MODELS["OpenAI"]
    token_limit = (
        {"max_completion_tokens": max_tokens}
        if resolved_model.startswith("gpt-5")
        else {"max_tokens": max_tokens}
    )
    resp = client.chat.completions.create(
        model=resolved_model,
        **token_limit,
        messages=[{"role": "user", "content": prompt}],
    )
    return resp.choices[0].message.content.strip()


def _call_gemini(api_key: str, prompt: str, max_tokens: int = 1000, model: str = None) -> str:
    from google import genai
    client = genai.Client(api_key=api_key)
    resp = client.models.generate_content(
        model=model or DEFAULT_MODELS["Gemini (free)"],
        contents=prompt,
    )
    return resp.text.strip()


def _call_mistral(api_key: str, prompt: str, max_tokens: int = 1000, model: str = None) -> str:
    from mistralai.client import Mistral
    client = Mistral(api_key=api_key)
    resp = client.chat.complete(
        model=model or DEFAULT_MODELS["Mistral (free tier)"],
        messages=[{"role": "user", "content": prompt}],
    )
    return resp.choices[0].message.content.strip()


def _call_groq(api_key: str, prompt: str, max_tokens: int = 1000, model: str = None) -> str:
    from groq import Groq
    client = Groq(api_key=api_key)
    resp = client.chat.completions.create(
        model=model or DEFAULT_MODELS["Groq (free tier)"],
        max_tokens=max_tokens,
        messages=[{"role": "user", "content": prompt}],
    )
    return resp.choices[0].message.content.strip()


_PROVIDER_FN = {
    "Claude": _call_claude,
    "OpenAI": _call_openai,
    "Gemini (free)": _call_gemini,
    "Mistral (free tier)": _call_mistral,
    "Groq (free tier)": _call_groq,
}


# ── Prompt builder ────────────────────────────────────────────────────────────

def _build_prompt(
    primary_keyword: str,
    supporting_keywords: list,
    page_template: str,
    business_type: str,
    brand_name: str,
    include_brand: bool,
    h1: str,
    word_count: int,
    paragraph_count: int,
    page_context: str,
    forbidden_phrases: str,
    brand_profile: dict = None,
    ai_overview_summary: str = "",
) -> str:
    biz_ctx = _BIZ_CONTEXT.get(business_type, _BIZ_CONTEXT["general"])
    template_rules = _TEMPLATE_RULES.get(page_template, _TEMPLATE_RULES["service_lp"])

    # Brand line
    if include_brand and brand_name:
        brand_line = f"Brand name: '{brand_name}'. Use exact casing. You may reference the brand naturally once if it fits the page type."
    elif brand_name:
        brand_line = f"Brand name: '{brand_name}'. Use exact casing if referenced but do NOT include the brand name in the copy."
    else:
        brand_line = "No brand name required."

    h1_line = f"Page H1 (context only — do not copy verbatim): {h1}" if h1 else ""

    # Forbidden phrases
    bp_avoid = (brand_profile or {}).get("words_to_avoid", "")
    combined_forbidden = ", ".join(filter(None, [forbidden_phrases.strip(), bp_avoid.strip()]))
    forbidden_line = f"Never use these phrases: {combined_forbidden}" if combined_forbidden else ""

    # Brand profile block
    bp_lines = []
    if brand_profile:
        if brand_profile.get("brand_voice"):
            bp_lines.append(f"Brand voice: {brand_profile['brand_voice']}")
        if brand_profile.get("tone"):
            bp_lines.append(f"Tone: {brand_profile['tone']}")
        if brand_profile.get("target_audience"):
            bp_lines.append(f"Target audience: {brand_profile['target_audience']}")
        if brand_profile.get("usps"):
            bp_lines.append(f"Unique selling points: {brand_profile['usps']}")
        if brand_profile.get("key_messages"):
            bp_lines.append(f"Key messages to reinforce: {brand_profile['key_messages']}")
        if brand_profile.get("competitors"):
            bp_lines.append(f"Competitors (differentiate from): {brand_profile['competitors']}")
        if brand_profile.get("example_copy"):
            bp_lines.append(f"Example copy to emulate in style (not content):\n{brand_profile['example_copy']}")
    brand_profile_block = ("BRAND CONTEXT:\n" + "\n".join(bp_lines)) if bp_lines else ""

    # Page context block
    context_block = ""
    if page_context:
        context_block = (
            "PAGE CONTENT (use this to understand what the page is about and write accurately to it):\n"
            f"---\n{page_context}\n---"
        )

    aio_block = ""
    if ai_overview_summary:
        aio_block = (
            "AI OVERVIEW FRAMING SIGNAL (search intent context only):\n"
            f"---\n{ai_overview_summary}\n---\n"
            "Use this only to understand likely search intent and authoritative subtopics. "
            "Do not copy, quote, or treat this as proof of this page's facts, policies, offers, or claims."
        )

    hook_structure_block = ""
    if page_template in ("blog", "brand"):
        hook_structure_block = """
BLOG AND BRAND HOOK STRUCTURE:
Choose the hook structure that best fits the topic:
1. Concrete outcome - start with what the reader will achieve.
2. Specific fact - open with a precise, relevant data point.
3. Direct assertion - make a clear, confident statement about the topic.
4. Problem frame - name a specific tension the reader already feels.
Make the chosen hook the first sentence, not preceded by a wind-up sentence.
""".strip()

    # Supporting keywords block
    if supporting_keywords:
        kw_list = "\n".join(f"- {kw}" for kw in supporting_keywords)
        kw_block = (
            f"Primary keyword (represent naturally in the opening paragraph): {primary_keyword}\n\n"
            f"Supporting keywords (weave up to 3 of these naturally into the copy where they fit. "
            f"A keyword used awkwardly is worse than not using it at all. "
            f"Quality of integration matters more than quantity):\n{kw_list}"
        )
    else:
        kw_block = f"Primary keyword (represent naturally in the opening paragraph): {primary_keyword}"

    # Word and paragraph targets
    para_instruction = (
        f"Write {paragraph_count} paragraph{'s' if paragraph_count > 1 else ''} "
        f"totalling approximately {word_count} words."
    )

    return f"""You are an expert SEO copywriter writing an introductory paragraph for a web page.

Your output is {word_count} words of polished, publication-ready copy. Nothing else — no preamble, no labels, no explanation.

KEYWORD REQUIREMENTS (SEO is the primary objective — 75% of quality signal):
{kw_block}

PAGE TYPE AND STRUCTURE:
{template_rules}

BUSINESS TYPE:
{biz_ctx}

{h1_line}
{brand_line}
{forbidden_line}

{brand_profile_block}

LENGTH AND FORMAT:
{para_instruction}
Target word count is a guide, not a hard cap. Vary sentence length. Do not pad to hit the number.

WRITING RULES:
- Represent the primary keyword naturally in the opening paragraph
- You may adjust word order, add small connecting words, or use a close grammatical variation when the exact keyword phrase would sound awkward
- Do not force the keyword at the beginning of the first sentence
- Do not start with a generic opener ("Welcome to", "Are you looking for", "In today's world", "Whether you are", "Finding the right", "When it comes to", "Choosing the right", "Looking for", "There are many", "It can be difficult to", "If you are searching for", "Whether you need", "In the world of")
- Do not write phrases like "this page", "on this page", or "the page" in the generated copy. Refer directly to the service, category, product, topic, brand, or location instead
- Do not use em dashes
- Write in active voice
- Every sentence must earn its place — no filler
- The copy must read as if written by a knowledgeable human, not generated
- Do not repeat the primary keyword more than twice total
- Supporting keywords should read as if the writer chose them, not placed them for SEO

ENGAGEMENT (secondary objective — 25% of quality signal):
- The first sentence must communicate the core topic, benefit, or value of the page. The reader should know what the page is about and why it matters to them after reading only the first sentence. The first sentence is not for warming up or establishing context; that is wasted space.
- For blog and brand templates, opening hook matters more than other types
- The final sentence can include a light forward-pointing idea, but it should name the topic directly instead of saying "this page"

{hook_structure_block}

{UNSUPPORTED_CLAIM_GUARDRAIL}

{SCRAPED_CONTEXT_GUARDRAIL}

{aio_block}

{context_block}

Return only the intro copy. No heading, no label, no surrounding quotes."""


# ── Main generation function ──────────────────────────────────────────────────

def generate_intro(
    provider: str,
    api_key: str,
    primary_keyword: str,
    supporting_keywords: list,
    page_template: str,
    business_type: str,
    brand_name: str,
    include_brand: bool,
    h1: str,
    word_count: int,
    paragraph_count: int,
    page_context: str,
    forbidden_phrases: str,
    model: str = None,
    brand_profile: dict = None,
    ai_overview_summary: str = "",
) -> str:
    fn = _PROVIDER_FN.get(provider)
    if not fn:
        raise ValueError(f"Unknown provider: {provider}")

    resolved_model = model or DEFAULT_MODELS.get(provider)
    prompt = _build_prompt(
        primary_keyword=primary_keyword,
        supporting_keywords=supporting_keywords,
        page_template=page_template,
        business_type=business_type,
        brand_name=brand_name,
        include_brand=include_brand,
        h1=h1,
        word_count=word_count,
        paragraph_count=paragraph_count,
        page_context=page_context,
        forbidden_phrases=forbidden_phrases,
        brand_profile=brand_profile or {},
        ai_overview_summary=ai_overview_summary,
    )

    max_tokens = 16384
    raw = fn(api_key, prompt, max_tokens=max_tokens, model=resolved_model)
    return sanitise(raw, brand_name)
