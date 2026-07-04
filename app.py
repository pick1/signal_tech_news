#!/usr/bin/env python3
"""
SIGNAL — Tech Intelligence Digest
==================================
A personal tech intelligence dashboard. Paste URLs, articles, Instagram posts,
or notes — SIGNAL fetches, analyzes, and produces structured reports.

Run: streamlit run app.py
"""

import json
import re
import time
import urllib.parse
from datetime import datetime

import streamlit as st
import os

from signald.config import (
    PROJECT_DIR,
    CAT_COLORS,
    CAT_LABELS,
)
from signald.db import get_db, load_entries, save_entry, delete_entry
from signald.auth import is_password_set, verify_password, set_password
from signald.scrapers import (
    detect_input_type,
    fetch_url_content,
    fetch_instagram_content,
    enrich_with_repos,
)
from signald.analyzer import analyze_content, run_enrichment, set_notification_callback


# ─── Wire notifications ──────────────────────────────────────────────────────
set_notification_callback(lambda msg, icon: st.toast(msg, icon=icon))


# ─── PAGE SETUP ───────────────────────────────────────────────────────────────
st.set_page_config(
    page_title="SIGNAL — Tech Intelligence",
    page_icon="/home/dp/projects/signal/static/favicon.svg",
    layout="wide",
    initial_sidebar_state="expanded",
)

st.markdown("""
<style>
@import url('https://fonts.googleapis.com/css2?family=Syne:wght@700;800&family=DM+Mono:ital,wght@0,300;0,400;1,300&display=swap');
html, body, [class*="css"] { font-family: 'DM Mono', monospace; }
.signal-header { font-family: 'Syne', sans-serif; font-size: 28px; font-weight: 800; color: #00ff88; letter-spacing: 0.15em; text-shadow: 0 0 30px rgba(0,255,136,0.4); margin-bottom: 0; }
.signal-sub { font-size: 10px; letter-spacing: 0.3em; color: #404060; text-transform: uppercase; margin-top: 0; }
.card { background: #111118; border: 1px solid #252535; border-radius: 12px; padding: 18px 20px; margin-bottom: 14px; border-left-width: 3px; }
.card-title { font-family: 'Syne', sans-serif; font-size: 17px; font-weight: 700; margin-bottom: 8px; }
.tag { display: inline-block; padding: 2px 10px; border-radius: 20px; font-size: 10px; letter-spacing: 0.1em; text-transform: uppercase; border: 1px solid; margin-right: 4px; margin-bottom: 4px; }
.section-label { font-size: 9px; letter-spacing: 0.2em; text-transform: uppercase; color: #404060; margin-bottom: 4px; }
.section-text { font-size: 12px; color: #9090b0; line-height: 1.6; }
.verdict-text { font-style: italic; font-size: 13px; color: #e8e8f0; line-height: 1.6; }
 </style>""", unsafe_allow_html=True)

# ── Inject clipboard handler for copy buttons ────────────────────────────────
st.components.v1.html(
    """
<script>
parent.document.addEventListener('click', function(e) {
    var btn = e.target.closest('.sl-copy-btn');
    if (!btn) return;
    var data = btn.getAttribute('data-clipboard');
    if (!data) return;
    navigator.clipboard.writeText(decodeURIComponent(data)).then(function() {
        btn.style.borderColor = '#00ff88';
        btn.textContent = 'Copied!';
        setTimeout(function() { btn.textContent = '\U0001f4cb Copy'; btn.style.borderColor = '#353550'; }, 1500);
    }).catch(function() {
        btn.textContent = 'Clipboard blocked';
        btn.style.borderColor = '#ff4466';
        setTimeout(function() { btn.textContent = '\U0001f4cb Copy'; btn.style.borderColor = '#353550'; }, 2000);
    });
});
</script>
""",
    height=0,
)


# ─── CARD RENDERER ────────────────────────────────────────────────────────────
def render_card(entry, color, label, conf, tags, date):
    cat_tag = '<span class="tag" style="color:' + color + ';border-color:' + color + ';background:rgba(0,0,0,0.3)">' + label + '</span>'

    topic_tags = ""
    for t in tags:
        topic_tags += '<span class="tag" style="color:#7070a0;border-color:#353550">' + t + '</span>'

    repo_tags = ""
    for r in entry.get("repos", []):
        name = r.get("full_name", "")
        url = r.get("url", "")
        if name:
            href = f' href="{url}" target="_blank"' if url else ""
            repo_tags += '<a' + href + '><span class="tag" style="color:#44ff88;border-color:#44ff8833;background:rgba(68,255,136,0.05)">' + name + '</span></a>'

    src_type = entry.get("source_type", "?")
    src_tag = '<span class="tag" style="color:#7070a0;border-color:#353550">' + src_type + '</span>'
    raw_url = entry.get("raw_url")

    next_steps_html = ""
    for s in entry.get("next_steps", []):
        next_steps_html += "▸ " + str(s) + "<br>"
    if not next_steps_html:
        next_steps_html = "none"

    opencode_fit = entry.get("opencode_fit", "not analyzed")
    verdict      = entry.get("verdict", "")
    summary      = entry.get("summary", "")
    impl         = entry.get("implementability", "")
    work_rel     = entry.get("work_relevance", "")
    title = entry.get("title", "Untitled")

    # ── AI copy prompt ──────────────────────────────────────────────────────
    next_steps_raw = entry.get("next_steps", [])
    ai_prompt = (
        f"{title}\n\n"
        f"Summary: {summary}\n\n"
        f"Verdict: {verdict}\n\n"
        f"Category: {label}\n"
        f"Tags: {', '.join(tags)}\n"
        f"Implementability: {impl}\n"
        f"Work Relevance: {work_rel}\n"
        f"Next Steps: {', '.join(next_steps_raw) if next_steps_raw else 'none'}\n"
        f"Source: {raw_url or src_type}"
    )
    encoded_prompt = urllib.parse.quote(ai_prompt)
    copy_btn = (
        f'<button class="sl-copy-btn" data-clipboard="{encoded_prompt}" '
        f'style="background:none;border:1px solid #353550;color:#44aaff;border-radius:4px;'
        f'padding:2px 8px;font-size:10px;cursor:pointer;margin-right:10px">'
        f'\U0001f4cb Copy</button>'
    )

    enrichment_data = entry.get("enrichment", {}) or {}
    enrichment_html = ""
    if enrichment_data and enrichment_data.get("github_results"):
        gh = enrichment_data["github_results"]
        findings = enrichment_data.get("key_findings", [])
        resources = enrichment_data.get("resources", [])
        enrich_time = enrichment_data.get("enrichment_time", 0)

        lines = ['<div style="margin-top:12px;background:#0f0f18;border:1px solid #33cc6633;border-radius:8px;padding:12px 14px">'
                 '<div class="section-label" style="color:#33cc66">\U0001f50d Enrichment</div>']

        # GitHub repos
        repo_items = []
        for r in gh[:4]:
            if "name" not in r:
                continue
            stars = r.get("stars", 0)
            desc = (r.get("description", "") or "")[:120]
            lang = r.get("language", "")
            lang_tag = f'<span style="color:#7070a0;font-size:10px;margin-left:4px">{lang}</span>' if lang else ""
            star_str = f'\U00002b50 {stars}' if stars else ""
            repo_items.append(
                f'<div style="margin:4px 0;font-size:12px">'
                f'<a href="{r["url"]}" target="_blank" style="color:#44aaff;text-decoration:none">{r["name"]}</a>'
                f'<span style="color:#808090"> {star_str}{lang_tag}</span><br>'
                f'<span style="color:#606080;font-size:11px">{desc}</span>'
                f'</div>'
            )
        if repo_items:
            lines.append('<div style="margin-top:8px"><span style="color:#33cc66;font-size:11px;font-weight:600">\U0001f4c1 GitHub Repos</span></div>')
            lines.extend(repo_items)

        # Key findings
        if findings:
            lines.append('<div style="margin-top:8px"><span style="color:#33cc66;font-size:11px;font-weight:600">\U0001f4dd Findings</span></div>')
            for f in findings[:3]:
                lines.append(f'<div style="color:#a0a0c0;font-size:11px;margin:3px 0">\u25b8 {f}</div>')

        # Resources
        if resources:
            lines.append('<div style="margin-top:8px"><span style="color:#33cc66;font-size:11px;font-weight:600">\U0001f517 Resources</span></div>')
            for r in resources[:3]:
                name = r.get("name", r.get("url", ""))
                url = r.get("url", "")
                desc = (r.get("description", "") or "")[:100]
                lines.append(
                    f'<div style="margin:3px 0;font-size:11px">'
                    f'<a href="{url}" target="_blank" style="color:#44aaff">{name}</a>'
                    f'<span style="color:#606080"> — {desc}</span></div>'
                )

        lines.append(f'<div style="color:#404060;font-size:10px;margin-top:6px">enriched in {enrich_time}s</div>')
        lines.append('</div>')
        enrichment_html = "".join(lines)

    instagram_html = ""
    meta = entry.get("instagram_meta")
    if meta:
        author = meta.get("author", "")
        likes = meta.get("likes", 0)
        caption = (meta.get("caption", "") or "")[:80]
        is_video = meta.get("is_video", False)
        has_transcript = bool(meta.get("transcript")) and not meta["transcript"].startswith("[transcription error")
        bits = []
        if author:
            bits.append(f"@{author}")
        if is_video:
            bits.append('<span class="tag" style="color:#ff4466;border-color:#ff446633;background:rgba(255,68,102,0.1)">REEL</span>')
        if likes:
            bits.append(f"likes: {likes}")
        if caption:
            bits.append(f'<span style="color:#606090">"{caption}{"..." if len((meta.get("caption") or "")) > 80 else ""}"</span>')
        if has_transcript:
            bits.append('<span class="tag" style="color:#44aaff;border-color:#44aaff33;background:rgba(68,170,255,0.1)">TRANSCRIPT</span>')
        if bits:
            instagram_html = '<div style="margin-bottom:10px;font-size:11px;color:#8080b0">' + " · ".join(bits) + '</div>'

    return (
        '<div class="card" style="border-left-color:' + color + '">'
        '<div class="card-title">' + title + '</div>'
        '<div style="margin-bottom:10px">' + cat_tag + src_tag + repo_tags + topic_tags + '</div>'
        + instagram_html +
        '<div class="section-text" style="margin-bottom:12px">' + summary + '</div>'
        '<div style="display:flex;gap:16px;margin-bottom:12px">'
          '<div style="flex:1;background:#0a0a0f;border:1px solid #252535;border-radius:8px;padding:10px 12px">'
            '<div class="section-label">Implementability</div>'
            '<div class="section-text">' + impl + '</div>'
          '</div>'
          '<div style="flex:1;background:#0a0a0f;border:1px solid #252535;border-radius:8px;padding:10px 12px">'
            '<div class="section-label">Work Relevance</div>'
            '<div class="section-text">' + work_rel + '</div>'
          '</div>'
        '</div>'
        '<div style="background:#0a0a0f;border:1px solid #252535;border-radius:8px;padding:10px 14px;margin-bottom:10px">'
          '<div class="section-label">Verdict</div>'
          '<div class="verdict-text">' + verdict + '</div>'
        '</div>'
        '<div style="display:flex;gap:16px;margin-bottom:10px">'
          '<div style="flex:1;background:#0a0a0f;border:1px solid #353550;border-radius:8px;padding:10px 12px">'
            '<div class="section-label">Next Steps</div>'
            '<div class="section-text">' + next_steps_html + '</div>'
          '</div>'
          '<div style="flex:1;background:#0a0a0f;border:1px solid #4488ff33;border-radius:8px;padding:10px 12px">'
            '<div class="section-label">OpenCode Fit</div>'
            '<div class="section-text">' + opencode_fit + '</div>'
          '</div>'
        '</div>'
        + enrichment_html +
        '<div style="display:flex;justify-content:space-between;align-items:center;border-top:1px solid #252535;padding-top:8px">'
          '<span style="color:#404060;font-size:11px">' + copy_btn + date + '</span>'
          '<span style="font-size:11px">'
            + (('<a href="' + raw_url + '" target="_blank" style="color:#44aaff;text-decoration:none;margin-right:10px">View source →</a>' if raw_url else '') or '<span style="color:#353550;font-size:10px;margin-right:10px">via ' + src_type + '</span>') +
            'confidence ' + str(conf) + '%'
            '<span style="display:inline-block;width:60px;height:4px;background:#252535;border-radius:2px;vertical-align:middle;margin-left:6px">'
              '<span style="display:block;width:' + str(conf) + '%;height:100%;background:' + color + ';border-radius:2px"></span>'
            '</span>'
          '</span>'
        '</div>'
        '</div>'
    )


# ─── AUTH GATE ────────────────────────────────────────────────────────────────
if not st.session_state.get("authenticated"):
    st.markdown(
        '<div class="signal-header" style="text-align:center;margin-top:40px">SIGNAL</div>'
        '<div class="signal-sub" style="text-align:center">Tech Intelligence Digest</div>',
        unsafe_allow_html=True,
    )

    if not is_password_set():
        st.markdown('<div style="max-width:360px;margin:40px auto">', unsafe_allow_html=True)
        st.markdown("**Set a password** to secure this dashboard.")
        pw = st.text_input("Password", type="password", key="setup_pw")
        pw2 = st.text_input("Confirm", type="password", key="setup_pw2")
        if st.button("Set Password", type="primary", use_container_width=True):
            if pw and pw == pw2 and len(pw) >= 4:
                set_password(pw)
                st.session_state["authenticated"] = True
                st.rerun()
            else:
                st.error("Password must be 4+ chars and match.")
        st.markdown("</div>", unsafe_allow_html=True)
    else:
        st.markdown('<div style="max-width:360px;margin:40px auto">', unsafe_allow_html=True)
        pw = st.text_input("Password", type="password", key="login_pw")
        if st.button("Unlock", type="primary", use_container_width=True):
            if verify_password(pw):
                st.session_state["authenticated"] = True
                st.rerun()
            else:
                st.error("Incorrect password.")
        st.markdown("</div>", unsafe_allow_html=True)

    st.stop()


# ─── SIDEBAR ──────────────────────────────────────────────────────────────────
with st.sidebar:
    st.markdown('<div class="signal-header">SIGNAL</div>', unsafe_allow_html=True)
    st.markdown('<div class="signal-sub">Tech Intelligence Digest</div>', unsafe_allow_html=True)
    st.divider()

    st.markdown("**Provider**")
    # Provider options: OpenRouter Free (primary) → OpenRouter Free (fallback) → Jetson LAN → Qwen Local
    OPENROUTER_BASE_URL = os.getenv("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1")
    OPENROUTER_API_KEY_1 = os.getenv("OPENROUTER_API_KEY")
    OPENROUTER_API_KEY_2 = os.getenv("OPENROUTER_API_KEY_FALLBACK")
    OPENROUTER_MODEL = os.getenv("OPENROUTER_MODEL", "google/gemini-pro-1.5")
    JETSON_BASE_URL = os.getenv("JETSON_BASE_URL", "http://192.168.1.110:8080/v1")
    JETSON_API_KEY = os.getenv("JETSON_API_KEY", "")
    JETSON_MODEL = os.getenv("JETSON_MODEL", "qwen3-14b")
    LOCAL_BASE_URL = os.getenv("LOCAL_LLM_BASE_URL", "http://localhost:11434/v1")
    LOCAL_API_KEY = os.getenv("LOCAL_LLM_API_KEY", "ollama")
    LOCAL_MODEL = os.getenv("LOCAL_LLM_MODEL", "qwen3:14b")

    provider_options = [
        {"label": "OpenRouter Free (primary)", "provider": "openai",
         "base_url": OPENROUTER_BASE_URL, "api_key": OPENROUTER_API_KEY_1, "model": OPENROUTER_MODEL},
        {"label": "OpenRouter Free (fallback)", "provider": "openai",
         "base_url": OPENROUTER_BASE_URL, "api_key": OPENROUTER_API_KEY_2, "model": OPENROUTER_MODEL},
        {"label": "Jetson LAN (Xavier)", "provider": "openai",
         "base_url": JETSON_BASE_URL, "api_key": JETSON_API_KEY, "model": JETSON_MODEL},
        {"label": "Qwen Local (Ollama)", "provider": "openai",
         "base_url": LOCAL_BASE_URL, "api_key": LOCAL_API_KEY, "model": LOCAL_MODEL},
    ]
    provider_labels = [opt["label"] for opt in provider_options]
    provider_choice_label = st.selectbox(
        "Provider", provider_labels,
        index=0,
        label_visibility="collapsed",
    )
    selected_opt = next(opt for opt in provider_options if opt["label"] == provider_choice_label)
    provider = selected_opt["provider"]
    base_url = selected_opt["base_url"]
    api_key = selected_opt["api_key"]
    model_override = selected_opt["model"]

    st.markdown("**Analysis Depth**")
    tier_options = ["Auto (per source type)", "Light", "Default", "Deep"]
    tier_choice = st.selectbox(
        "Tier", tier_options,
        index=0,
        label_visibility="collapsed",
    )
    tier_map = {"Auto (per source type)": None, "Light": "light", "Default": "default", "Deep": "deep"}
    tier_override = tier_map[tier_choice]

    st.markdown("**Enrichment**")
    run_enrich = st.checkbox("Research entities + find repos", value=True,
                              help="After analysis, extracts tools/repos and searches GitHub for real resources")

    if st.button("Lock", use_container_width=True):
        st.session_state["authenticated"] = False
        st.rerun()

    st.divider()

    st.markdown("**New Entry**")
    input_text = st.text_area(
        "Paste content or URL",
        height=180,
        placeholder="Paste article text, a URL, Instagram post, tweet thread...\n\nURLs are fetched and scraped automatically.",
        label_visibility="collapsed",
    )
    source_override = st.selectbox("Source type", [
        "Auto-detect", "Article", "URL", "Instagram", "Tweet", "Newsletter", "Note"
    ])
    analyze_btn = st.button("ANALYZE", use_container_width=True, type="primary")

    st.divider()

    st.markdown("**Filter Feed**")
    search_query = st.text_input("Search", placeholder="keyword...", label_visibility="collapsed")
    cat_filter = st.multiselect(
        "Categories",
        options=list(CAT_LABELS.keys()),
        format_func=lambda x: CAT_LABELS[x],
        label_visibility="collapsed",
    )

    st.divider()

    entries_all = load_entries()
    if entries_all:
        md_lines = ["# SIGNAL — Tech Intelligence Digest", "_Exported " + datetime.now().strftime("%Y-%m-%d") + "_", ""]
        for cat, label in CAT_LABELS.items():
            group = [e for e in entries_all if e.get("category") == cat]
            if not group:
                continue
            md_lines += ["## " + label, ""]
            for e in group:
                steps = "\n".join("- " + s for s in e.get("next_steps", []))
                raw_exp_conf = e.get("confidence", 0.8)
                if isinstance(raw_exp_conf, str):
                    try:
                        raw_exp_conf = float(raw_exp_conf)
                    except ValueError:
                        raw_exp_conf = 0.8
                md_lines += [
                    "### " + e.get("title", "Untitled"),
                    "**Tags:** " + ", ".join(e.get("tags", [])) + " | **Date:** " + e.get("created_at", "")[:10],
                    "", e.get("summary", ""), "",
                    "**Implementability:** " + e.get("implementability", ""),
                    "**Work Relevance:** " + e.get("work_relevance", ""),
                    "**Verdict:** _" + e.get("verdict", "") + "_",
                    "**Next Steps:**\n" + steps,
                    "**OpenCode Fit:** " + e.get("opencode_fit", ""),
                    "**Confidence:** " + str(round(raw_exp_conf * 100)) + "%",
                    "", "---", ""
                ]
        st.download_button("Export Markdown", "\n".join(md_lines),
                           file_name="signal-digest.md", mime="text/markdown",
                           use_container_width=True)
        st.download_button("Export JSON", json.dumps(entries_all, indent=2),
                           file_name="signal-digest.json", mime="application/json",
                           use_container_width=True)
        if st.button("Clear All", use_container_width=True):
            if st.session_state.get("confirm_clear"):
                get_db().truncate()
                st.session_state.confirm_clear = False
                st.rerun()
            else:
                st.session_state.confirm_clear = True
                st.warning("Click again to confirm.")


# ─── ANALYSIS TRIGGER ─────────────────────────────────────────────────────────
if analyze_btn and input_text.strip():
    raw_input = input_text.strip()
    detected = detect_input_type(raw_input) if source_override == "Auto-detect" else source_override.lower()

    content = raw_input
    fetch_notice = None
    instagram_meta = None
    repo_data = []

    if re.match(r"https?://", raw_input):
        if detected == "instagram":
            with st.spinner("Fetching Instagram post..."):
                content, instagram_meta = fetch_instagram_content(raw_input)
                notice_parts = ["Fetched Instagram post"]
                if instagram_meta:
                    m = instagram_meta
                    if m.get("is_video"):
                        notice_parts.append("reel")
                    if m.get("transcript") and not m["transcript"].startswith("[transcription error"):
                        notice_parts.append("audio transcript")
                fetch_notice = " · ".join(notice_parts) + f" ({len(content)} chars)"
        else:
            with st.spinner("Fetching " + raw_input[:60] + "..."):
                content = fetch_url_content(raw_input)
                fetch_notice = "Fetched " + str(len(content)) + " chars from URL"

    # Scan all content for repo references and auto-fetch metadata
    if content and not content.startswith("[Could not"):
        enriched, repo_data = enrich_with_repos(content, raw_input)
        if repo_data:
            content = enriched
            n = len(repo_data)
            fetch_notice = (fetch_notice or "Ready") + f" · {n} repo{'s' if n > 1 else ''} found"

    with st.spinner(f"Analyzing via {provider_choice_label}..."):
        try:
            result = analyze_content(
                content, detected, provider, tier_override,
                base_url=base_url, model_override=model_override, api_key=api_key,
            )
            enrichment = run_enrichment(content, result, provider) if run_enrich else None
            entry = {
                "id": str(int(time.time() * 1000)),
                "created_at": datetime.now().isoformat(),
                "raw_url": raw_input if re.match(r"https?://", raw_input) else None,
                "source_type": detected,
                "instagram_meta": instagram_meta,
                "repos": repo_data,
                **result,
                "enrichment": enrichment or {},
            }
            save_entry(entry)
            if fetch_notice:
                st.toast(fetch_notice, icon="✅")
            st.toast("Entry added to feed", icon="📥")
            st.rerun()
        except Exception as e:
            st.error("Analysis failed: " + str(e))

elif analyze_btn:
    st.sidebar.warning("Paste some content first.")


# ─── MAIN FEED ────────────────────────────────────────────────────────────────
entries = load_entries()

if cat_filter:
    entries = [e for e in entries if e.get("category") in cat_filter]
if search_query:
    q = search_query.lower()
    entries = [e for e in entries if
               q in e.get("title", "").lower() or
               q in e.get("summary", "").lower() or
               q in e.get("verdict", "").lower() or
               any(q in t.lower() for t in e.get("tags", []))]

all_entries = load_entries()

from signald.config import CAT_LABELS

# Build dynamic metric columns for all categories that have actual entries
cat_counts = {}
for e in all_entries:
    cat = e.get("category", "none") or "none"
    cat_counts[cat] = cat_counts.get(cat, 0) + 1

# Build column list: Total + every category with entries (in defined order)
metric_categories = [c for c in CAT_LABELS if cat_counts.get(c, 0) > 0]
num_cols = 1 + len(metric_categories)
cols = st.columns(num_cols)
with cols[0]:
    st.metric("Total", len(all_entries))
for i, cat in enumerate(metric_categories):
    with cols[i + 1]:
        st.metric(CAT_LABELS[cat], cat_counts.get(cat, 0))

st.divider()

feed_col, _ = st.columns([3, 0.01])

with feed_col:
    st.markdown("**Intelligence Feed** — " + str(len(entries)) + (" entry" if len(entries) == 1 else " entries"))

    if not entries:
        st.info("Feed is empty. Paste an article, URL, or post in the sidebar and hit Analyze.")
    else:
        for entry in entries:
            cat    = entry.get("category", "watch")
            color  = CAT_COLORS.get(cat, "#888")
            label  = CAT_LABELS.get(cat, cat)
            raw_conf = entry.get("confidence", 0.8)
            if isinstance(raw_conf, str):
                try:
                    raw_conf = float(raw_conf)
                except ValueError:
                    raw_conf = 0.8
            conf = round(raw_conf * 100)
            tags   = entry.get("tags", [])
            date   = entry.get("created_at", "")[:10]
            doc_id = entry.doc_id

            st.markdown(render_card(entry, color, label, conf, tags, date), unsafe_allow_html=True)

            if st.button("Delete", key="del_" + str(doc_id), help="Remove this entry"):
                delete_entry(doc_id)
                st.rerun()

            st.markdown("<div style='margin-bottom:8px'></div>", unsafe_allow_html=True)
