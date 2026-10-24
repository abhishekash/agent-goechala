"""Human-readable uncertainty shared by session and trace inspectors."""


def analysis_coverage(summary):
    notes = []
    if summary.get('usage_complete') is False:
        notes.append('Usage and cost analysis is incomplete under payload budgets; unknown totals are not zero.')
    if summary.get('status_complete') is False:
        notes.append('Status analysis is partial; excluded payloads may contain additional errors or lifecycle evidence.')
    if (summary.get('evidence_window') or {}).get('complete') is False:
        notes.append('Findings use a bounded evidence window, not complete trace history.')
    if summary.get('search_terms_complete') is False:
        notes.append('Trace search terms are partial. Use Activity history to investigate older events.')
    return ' '.join(notes)
