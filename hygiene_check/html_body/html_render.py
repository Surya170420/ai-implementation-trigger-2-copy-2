import re
from html import escape
from datetime import datetime
from hygiene_check.html_body.query_retriver import DataQualityQueryBuilder

sql_builder = DataQualityQueryBuilder()

# Causes that mean "this isn't actually a scraper bug" -> exclude from the
# actionable list and show separately instead. Belt-and-suspenders check:
# these should already be true_positive=false and filtered out upstream by
# decide_escalation, but if that ever lets one through, don't let it reach
# the actionable table anyway.
NON_BUG_CAUSES = {"legitimate", "not_applicable"}

# How many sample keywords/crawls to show per issue instead of the full list.
SAMPLE_SIZE = 8

# If the report's own failed_share disagrees with the "X of Y rows failed"
# text in its summary/detail by more than this, flag it as inconsistent.
SHARE_MISMATCH_THRESHOLD = 0.15

def _implied_share(text):
    """Pull the first 'X of Y rows failed' out of a summary/detail string
    and return X/Y, or None if no such pattern is found."""
    if not text:
        return None
    match = re.search(r"(\d+)\s+of\s+(\d+)\s+rows failed", text)
    if not match:
        return None
    x, y = int(match.group(1)), int(match.group(2))
    return x / y if y else None


def _share_mismatch_note(reported_share, text):
    implied = _implied_share(text)
    if implied is None or reported_share is None:
        return None
    if abs(implied - reported_share) > SHARE_MISMATCH_THRESHOLD:
        return (
            f"Report inconsistency: failed_share is reported as {reported_share:.0%}, "
            f"but the detail text implies ~{implied:.0%} ('{escape(text)}'). "
            f"One of these numbers is wrong in the source report."
        )
    return None


def _format_column(column):
    """alert['column'] is normally a single string (e.g. 'num_of_rating').
    Only join with commas if it's actually a list/tuple -- joining a plain
    string iterates its characters ('n, u, m, ...'), which was the bug."""
    if isinstance(column, (list, tuple)):
        return ", ".join(map(str, column))
    return str(column) if column else "None"


def _sample_crawls(crawls_affected):
    """Return (total_count, distinct_sample) for a crawls_affected list.
    Handles both keyword-style entries and date/hour-style entries."""
    if not crawls_affected:
        return 0, []

    seen = []
    for c in crawls_affected:
        if "keyword" in c:
            label = c["keyword"]
        elif "date_stamp" in c or "hour_stamp" in c:
            label = f"{c.get('date_stamp', '?')} hour {c.get('hour_stamp', '?')}"
        else:
            label = str(c)
        if label not in seen:
            seen.append(label)
        if len(seen) >= SAMPLE_SIZE:
            break

    return len(crawls_affected), seen


def _impact_score(alert):
    """Sort key: bigger failed_share and higher recurrence = more urgent.
    Deliberately ignores the report's own severity label, which doesn't
    track actual impact (e.g. a 34.7%-failure item is tagged 'warn')."""
    return (alert.get("failed_share") or 0, alert.get("recurrence") or 0, alert.get("failed_rows") or 0)


def _format_sql_multiline(sql_string):
    """Formats SQL into multi-line using HTML tags so email clients cannot ignore it."""
    if not sql_string or sql_string.startswith("--"):
        return escape(str(sql_string))

    # Collapse the source query's own whitespace/newlines first. The query
    # builder's raw output already has its own line breaks (and .sql-container
    # is white-space:pre), so without this the source's blank lines render
    # literally AND stack with the <br> tags we inject below -- that's the
    # doubled blank-line spacing.
    collapsed = re.sub(r'\s+', ' ', str(sql_string)).strip()

    # 1. Escape the raw string first so we don't accidentally escape our <br> tags later
    formatted = escape(collapsed)
    
    # 2. Inject <br> before major SQL clauses
    major_keywords = [
        'SELECT', 'FROM', 'WHERE', 'LEFT JOIN', 'INNER JOIN', 'RIGHT JOIN', 
        'JOIN', 'GROUP BY', 'ORDER BY', 'HAVING', 'LIMIT', 'UNION'
    ]
    for kw in major_keywords:
        # Adds a line break before the keyword
        formatted = re.sub(fr'\b({kw})\b', rf'<br>\1', formatted, flags=re.IGNORECASE)
        
    # 3. Inject <br> and 4 non-breaking spaces before logic conditions
    minor_keywords = ['AND', 'OR']
    for kw in minor_keywords:
        # Adds a line break and indentation
        formatted = re.sub(fr'\b({kw})\b', rf'<br>&nbsp;&nbsp;&nbsp;&nbsp;\1', formatted, flags=re.IGNORECASE)
        
    # 4. Cleanup: Remove the very first <br> if the string started with SELECT
    if formatted.startswith('<br>'):
        formatted = formatted[4:]
        
    # Cleanup: Prevent accidental double-spacing
    formatted = formatted.replace('<br><br>', '<br>')
    
    return formatted


def render_email_html(payload):
    alerts = payload.get("alerts", [])
    digest = payload.get("digest", [])

    actionable = [a for a in alerts if a.get("cause") not in NON_BUG_CAUSES]
    excluded = [a for a in alerts if a.get("cause") in NON_BUG_CAUSES]
    actionable.sort(key=_impact_score, reverse=True)
    
    run_date = payload.get("date", "")
    time_range = payload.get("hour_range", "")
    llm_model = payload.get("llm_model") or "not run (--no-llm)"
    run_id = payload.get("run_id") or "n/a"
    
    # Extract numeric start and end hours for SQL Builder from the time_range string (e.g. "1-4")
    try:
        hours = [int(h) for h in re.findall(r'\d+', str(time_range))]
        sql_time_range = (hours[0], hours[1]) if len(hours) >= 2 else (0, 23)
    except:
        sql_time_range = (0, 23)

    html = []
    html.append("""
    <html>
    <head>
        <style>
            body { font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif; background-color: #f3f4f6; margin: 0; padding: 20px; color: #333; }
            .container { max-width: 1000px; margin: auto; background: #ffffff; border-radius: 12px; box-shadow: 0 8px 16px rgba(0,0,0,0.05); overflow: hidden; }
            .header { background: linear-gradient(135deg, #4f46e5 0%, #3b82f6 100%); color: white; padding: 25px; text-align: center; }
            .header h2 { margin: 0; font-size: 24px; font-weight: 600; letter-spacing: 0.5px; }
            .stats-container { padding: 20px; display: table; width: 100%; box-sizing: border-box; background: #f8fafc; border-bottom: 1px solid #e2e8f0; }
            .stat-box { display: table-cell; text-align: center; padding: 10px; }
            .stat-value { font-size: 22px; font-weight: bold; color: #1e293b; }
            .stat-label { font-size: 12px; color: #64748b; text-transform: uppercase; letter-spacing: 1px; margin-top: 4px; }
            .content { padding: 30px; }
            .table-styled { width: 100%; border-collapse: collapse; margin-bottom: 30px; box-shadow: 0 2px 4px rgba(0,0,0,0.02); }
            .table-styled th { background-color: #f1f5f9; color: #475569; font-size: 13px; text-transform: uppercase; padding: 12px 15px; text-align: left; border-bottom: 2px solid #cbd5e1; }
            .table-styled td { padding: 12px 15px; border-bottom: 1px solid #e2e8f0; font-size: 14px; }
            .table-styled tr:nth-child(even) { background-color: #f8fafc; }
            .issue-card { background: #ffffff; border: 1px solid #e2e8f0; border-radius: 8px; margin-bottom: 25px; box-shadow: 0 4px 6px rgba(0,0,0,0.02); overflow: hidden; }
            .issue-header { background: #fee2e2; padding: 15px 20px; border-bottom: 1px solid #fecaca; display: flex; align-items: center; }
            .issue-header h2 { margin: 0; color: #991b1b; font-size: 18px; }
            .issue-body { padding: 20px; }
            .sql-container { background: #1e1e1e; color: #d4d4d4; padding: 15px; border-radius: 6px; font-family: 'Consolas', 'Courier New', monospace; font-size: 13px; overflow-x: auto; margin: 15px 0; border-left: 4px solid #3b82f6; white-space: pre;}
            .info-table td { padding: 8px 10px; border-bottom: 1px dashed #e2e8f0; font-size: 14px; }
            .info-table td:first-child { font-weight: 600; color: #475569; width: 30%; }
            .badge { display: inline-block; padding: 4px 8px; border-radius: 4px; font-size: 12px; font-weight: bold; }
            .badge-red { background: #fee2e2; color: #991b1b; }
            .badge-blue { background: #dbeafe; color: #1e40af; }
            .badge-gray { background: #f1f5f9; color: #475569; }
            .alert-box { padding: 12px; border-radius: 6px; margin: 15px 0; font-size: 14px; }
            .alert-warning { background: #fffbeb; border-left: 4px solid #f59e0b; color: #92400e; }
            .alert-danger { background: #fef2f2; border-left: 4px solid #ef4444; color: #991b1b; }
            .alert-success { background: #f0fdf4; border-left: 4px solid #22c55e; color: #166534; }
        </style>
    </head>
    <body>
    <div class="container">
    """)

    html.append(f"""
    <div class="header">
        <h2>🚨 Scraper Validation Quality Report</h2>
    </div>
    
    <div class="stats-container">
        <div class="stat-box">
            <div class="stat-value">{run_date}</div>
            <div class="stat-label">Run Date</div>
        </div>
        <div class="stat-box">
            <div class="stat-value">{time_range}</div>
            <div class="stat-label">Time Window</div>
        </div>
        <div class="stat-box">
            <div class="stat-value" style="font-size:15px;">{escape(str(llm_model))}</div>
            <div class="stat-label">Model</div>
        </div>
        <div class="stat-box">
            <div class="stat-value" style="font-size:13px; font-family:'Consolas','Courier New',monospace;">{escape(str(run_id))}</div>
            <div class="stat-label">Run ID</div>
        </div>
        <div class="stat-box">
            <div class="stat-value" style="color: #dc2626;">{len(actionable)}</div>
            <div class="stat-label">Actionable Issues</div>
        </div>
    </div>
    <div class="content">
    <p style="font-size:13px; color:#64748b; margin-top:0; margin-bottom: 25px;">
    <i>Issues are ordered by actual data impact (failed row share, then recurrence) rather than the report's own severity label.</i>
    </p>
    """)

    # ---------------- Summary Table ----------------
    html.append("""
    <h3 style="color: #334155; border-bottom: 2px solid #e2e8f0; padding-bottom: 8px;">Issue Summary</h3>
    <table class="table-styled">
    <tr>
        <th>#</th>
        <th>Validation Check</th>
        <th>Columns</th>
        <th>Failed Rows</th>
        <th>Failed Share</th>
        <th>Root Cause</th>
    </tr>
    """)

    for i, alert in enumerate(actionable, 1):
        platform = alert.get("platfrom_name", "Unknown")
        rule = alert.get("rule_name", "Unknown")
        table = alert.get("table_name", "Unknown")
        cols = escape(_format_column(alert.get("column")))

        raw_cause = (alert.get("cause") or "").strip()

        if raw_cause:
            cause = escape(raw_cause)
        else:
            cause = "Pending Investigation"

        html.append(f"""
        <tr>
            <td><b>{i}</b></td>
            <td>
                <span class="badge badge-blue">{escape(table)}</span><br>
                {escape(platform)} | <b>{escape(rule)}</b>
            </td>
            <td style="color:#64748b; font-size:13px;">{cols}</td>
            <td style="font-weight:bold; color:#ef4444;">{alert['failed_rows']:,}</td>
            <td><span class="badge badge-red">{alert['failed_share']:.2%}</span></td>
            <td>{cause}</td>
        </tr>
        """)

    html.append("</table>")


    # ---------------- Details ----------------
    html.append("""<h3 style="color: #334155; border-bottom: 2px solid #e2e8f0; padding-bottom: 8px; margin-top:40px;">Deep Dive Details</h3>""")
    
    for i, alert in enumerate(actionable, 1):
        
        # SQL Generation
        try:
            raw_sql = sql_builder.retrieve_query(
                table_name=alert.get('table_name', ''),
                rule_name=alert.get('rule_name', ''),
                date=run_date,
                time_range=sql_time_range,
                platform=alert.get('platfrom_name', 'Amazon')
            )
            # Format the query for better readability using HTML tags
            sql_query = _format_sql_multiline(raw_sql)
            
        except Exception as e:
            sql_query = f"-- [Query Builder Error]: {escape(str(e))}<br>-- Ensure rule '{escape(alert.get('rule_name', ''))}' is mapped in DataQualityQueryBuilder."
            
        html.append(f"""
        <div class="issue-card">
            <div class="issue-header">
                <h2>Issue #{i}: {escape(alert.get('rule_name', 'Unknown Rule'))}</h2>
            </div>
            <div class="issue-body">
                
                <h4 style="margin:0 0 10px 0; color:#475569;">Diagnostic SQL Query</h4>
                <div class="sql-container" style="line-height: 1.6;">{sql_query}</div>

                <table class="info-table" style="width: 100%; border-collapse: collapse; margin-bottom: 20px;">
                    <tr><td>Platform / Table</td><td><b>{escape(alert.get('platfrom_name', ''))}</b> / {escape(alert.get('table_name', ''))}</td></tr>
                    <tr><td>Affected Column(s)</td><td>{escape(_format_column(alert.get('column')))}</td></tr>
                    <tr><td>Reported Severity</td><td><span class="badge badge-gray">{escape(alert.get("severity", "N/A"))}</span></td></tr>
                    <tr><td>Investigated By</td><td>{escape(str(alert.get("model") or "not investigated"))}</td></tr>
                    <tr><td>Failed Rows</td><td style="color: #dc2626; font-weight: bold;">{alert.get("failed_rows", 0):,}</td></tr>
                    <tr><td>Failed Share</td><td>{alert.get("failed_share", 0):.2%}</td></tr>
                    <tr><td>Summary Message</td><td style="color: #475569;"><i>{escape(alert.get("summary", ""))}</i></td></tr>
                </table>
        """)

        mismatch_note = _share_mismatch_note(alert.get("failed_share"), alert.get("summary"))
        if mismatch_note:
            html.append(f"""
            <div class="alert-box alert-danger">
                <b>⚠️ Data Mismatch:</b> {mismatch_note}
            </div>
            """)

        if alert.get("cause"):
            confidence = alert.get("confidence")
            conf_text = f" <span style='color:#b45309; font-size:12px;'>(Confidence: {confidence:.0%})</span>" if confidence is not None else ""
            html.append(f"""
            <div class="alert-box alert-warning">
                <b>🔍 Root Cause:</b> {escape(alert["cause"])}{conf_text}
            </div>
            """)
        else:
            html.append("""
            <div class="alert-box" style="background:#f1f5f9; border-left:4px solid #94a3b8; color:#475569;">
                <b>🔍 Root Cause:</b> Not yet investigated — requires human review.
            </div>
            """)

        current_xp = alert.get("current_xpath")
        suggested_xp = alert.get("suggested_xpath")
        if current_xp and suggested_xp and current_xp != suggested_xp:
            html.append(f"""
            <h4 style="margin: 15px 0 5px 0; color:#475569;">XPath Drift</h4>
            <table style="width:100%; border-collapse:collapse; font-size:13px;">
                <tr>
                    <td style="width:90px; color:#64748b; vertical-align:top; padding:6px 8px 6px 0;">Current (broken)</td>
                    <td><pre style="margin:0; background:#fef2f2; padding:10px; border:1px solid #fecaca; border-radius:4px; overflow-x:auto;">{escape(current_xp)}</pre></td>
                </tr>
                <tr>
                    <td style="color:#64748b; vertical-align:top; padding:6px 8px 6px 0;">Suggested</td>
                    <td><pre style="margin:0; background:#f0fdf4; padding:10px; border:1px solid #bbf7d0; border-radius:4px; overflow-x:auto;">{escape(suggested_xp)}</pre></td>
                </tr>
            </table>
            """)
        elif suggested_xp:
            html.append(f"""
            <h4 style="margin: 15px 0 5px 0; color:#475569;">Suggested XPath</h4>
            <pre style="background:#f8fafc; padding:12px; border:1px solid #cbd5e1; border-radius:4px; overflow-x:auto; font-size:13px;">{escape(suggested_xp)}</pre>
            """)
        elif current_xp:
            html.append(f"""
            <h4 style="margin: 15px 0 5px 0; color:#475569;">Current XPath (unable to confirm a fix)</h4>
            <pre style="background:#f8fafc; padding:12px; border:1px solid #cbd5e1; border-radius:4px; overflow-x:auto; font-size:13px;">{escape(current_xp)}</pre>
            """)

        total, sample = _sample_crawls(alert.get("crawls_affected"))
        if total:
            html.append(f"<h4 style='margin: 15px 0 5px 0; color:#475569;'>Affected Crawls ({total:,} total, showing up to {SAMPLE_SIZE})</h4>")
            html.append("<ul style='margin-top:5px; color:#475569; font-size:14px;'>")
            for label in sample:
                html.append(f"<li>{escape(label)}</li>")
            html.append("</ul>")
            
        html.append("</div></div>") # Close issue-body and issue-card

    # ---------------- Excluded (not a bug) ----------------
    if excluded:
        html.append("<h3 style='color: #15803d; border-bottom: 2px solid #bbf7d0; padding-bottom: 8px; margin-top:40px;'>Excluded Data (Not a Scraper Bug)</h3>")
        for alert in excluded:
            confidence = alert.get("confidence")
            conf_text = f" (Confidence: {confidence:.0%})" if confidence is not None else ""
            html.append(f"""
            <div class="alert-box alert-success" style="margin-bottom:15px;">
                <b style="font-size:16px;">{escape(alert.get('subject', 'Unknown Subject'))}</b><br>
                <span style="font-size:13px; color:#166534; display:block; margin: 5px 0;">{alert.get('failed_rows', 0):,} rows flagged.</span>
                <i style="color:#14532d;">{escape(alert.get('summary', ''))}</i><br>
                <div style="margin-top: 8px; font-weight:500;">Cause: {escape(alert.get('cause', ''))} {conf_text}</div>
            </div>
            """)

    html.append("""
        </div> <!-- End content -->
    </div> <!-- End container -->
    </body>
    </html>
    """)
    return "".join(html)