import html
import json
from pathlib import Path


def render_brief(
    *,
    run_id: str,
    coverage_name: str,
    coverage_version: str,
    thesis: str,
    published_signals: list[dict],
    held_signals: list[dict],
    excluded_events: list[dict] | None = None,
    output_path: Path,
) -> dict:
    excluded_events = excluded_events or []
    material = any(item["signal_level"] in {"S2", "S3"} for item in published_signals)
    summary = "MATERIAL_CHANGE" if material else "NO_MATERIAL_CHANGE"
    published_ids = [item["id"] for item in published_signals]
    cards: list[str] = []
    for item in published_signals + held_signals:
        facts = "".join(f"<li>{html.escape(f['statement'])}</li>" for f in item["analysis"]["facts"])
        source_line = ", ".join(
            f"{html.escape(s['tier'])} — {html.escape(s['name'])} "
            f"({html.escape(s['published_at'][:10])}) — "
            f"{html.escape(s.get('url') or 'N/A')}"
            for s in item["sources"]
        )
        red_team = item.get("red_team") or {}
        disposition = "PUBLISHABLE" if item in published_signals else "HELD"
        cards.append(
            f"""
            <article data-signal-id="{item['id']}">
              <h2>{html.escape(item['signal_level'])}: {html.escape(item['event_title'])}</h2>
              <p><strong>Disposition</strong>: {disposition}</p>
              <p><strong>One-line conclusion</strong>: {html.escape(item['analysis']['thesis_impact'])}</p>
              <p><strong>Verified facts</strong></p><ul>{facts or '<li>N/A</li>'}</ul>
              <p><strong>Sources</strong>: {source_line}</p>
              <p><strong>Marginal change</strong>: {html.escape(item['analysis']['marginal_change'])}</p>
              <p><strong>Affected variable</strong>: {html.escape(item['affected_variable'])}</p>
              <p><strong>Thesis impact</strong>: {html.escape(item['analysis']['thesis_impact'])}</p>
              <p><strong>Red Team</strong>: {html.escape(red_team.get('action', 'N/A'))}; {html.escape(red_team.get('alternative_explanation', 'N/A'))}</p>
              <p><strong>Unknowns</strong>: {html.escape('; '.join(item['analysis']['unknowns']) or 'N/A')}</p>
              <p><strong>Next validation</strong>: {html.escape(item['analysis']['next_validation'])}</p>
              <p><strong>Adjudication</strong>: {html.escape(item['adjudication'])}</p>
            </article>
            """
        )
    held = "".join(
        f"<li>{html.escape(item['event_title'])}: {html.escape(item['pipeline_state'])} / {html.escape(item['adjudication'] or 'N/A')}</li>"
        for item in held_signals
    )
    excluded = "".join(
        f"""
        <article data-event-state="OUT_OF_SCOPE">
          <h2>OUT_OF_SCOPE: {html.escape(item['event_title'])}</h2>
          <p><strong>Disposition</strong>: DROP</p>
          <p><strong>One-line conclusion</strong>: {html.escape(item['analysis']['thesis_impact'])}</p>
          <p><strong>Verified facts</strong></p><ul>{''.join(f"<li>{html.escape(fact['statement'])}</li>" for fact in item['analysis']['facts']) or '<li>N/A</li>'}</ul>
          <p><strong>Sources</strong>: {', '.join(f"{html.escape(source['tier'])} — {html.escape(source['name'])} ({html.escape(source['published_at'][:10])}) — {html.escape(source.get('url') or 'N/A')}" for source in item['sources'])}</p>
          <p><strong>Marginal change</strong>: {html.escape(item['analysis']['marginal_change'])}</p>
          <p><strong>Affected variable</strong>: {html.escape(item['analysis']['affected_variable'])}</p>
          <p><strong>Thesis impact</strong>: {html.escape(item['analysis']['thesis_impact'])}</p>
          <p><strong>Signal level</strong>: {html.escape(item['analysis']['signal_level'])}</p>
          <p><strong>Red Team</strong>: N/A</p>
          <p><strong>Unknowns</strong>: {html.escape('; '.join(item['analysis']['unknowns']) or 'N/A')}</p>
          <p><strong>Next validation</strong>: {html.escape(item['analysis']['next_validation'])}</p>
          <p><strong>Adjudication</strong>: DROP</p>
        </article>
        """
        for item in excluded_events
    )
    body = "".join(cards) or "<p>No publishable signals in this run.</p>"
    document = f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>AI Intelligence Monitor {html.escape(run_id)}</title>
<style>body{{font-family:system-ui;max-width:900px;margin:40px auto;line-height:1.55}}article{{border-top:1px solid #bbb;padding:16px 0}}code{{background:#eee;padding:2px 4px}}</style></head>
<body>
<!-- published-signal-ids:{json.dumps(published_ids, separators=(',', ':'))} -->
<!-- brief-summary:{summary} -->
<!-- ops-check-status:PENDING -->
<h1>Run {html.escape(run_id)}</h1>
<p><strong>Coverage</strong>: {html.escape(coverage_name)} v{html.escape(coverage_version)}</p>
<p><strong>Run result</strong>: <code>{summary}</code></p>
<p><strong>Ops Check</strong>: <code data-ops-check-status>PENDING</code></p>
<p><strong>Thesis</strong>: {html.escape(thesis)}</p>
<section><h2>Signals</h2>{body}</section>
<section><h2>Held from automatic publication</h2><ul>{held or '<li>None</li>'}</ul></section>
<section><h2>Excluded by coverage gate</h2>{excluded or '<p>None</p>'}</section>
</body></html>"""
    output_path.write_text(document, encoding="utf-8")
    return {"summary": summary, "published_signal_ids": published_ids}
