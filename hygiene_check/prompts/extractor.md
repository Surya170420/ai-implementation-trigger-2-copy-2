You are reading raw scraper HTML for one product result and comparing it,
column by column, against the value the scraper actually stored in the
database. You are NOT deciding whether this is a real problem — only
reporting what each column's true value is in the HTML, and whether it
matches the stored value. A second reviewer will make the final call.

## Stored row (what the scraper wrote to the table)
{{ row_data }}

## Columns to check
{{ columns }}

## Candidate xpaths from the crawler's own source code (variable_name -> list of xpaths it uses)
{{ candidate_xpaths }}
These come straight from the scraper's Python source, extracted automatically
by variable name — there is no guaranteed mapping from a Python variable name
to one of the "columns to check" above (e.g. a variable called `price_xpath`
is probably the current xpath for column `sp`, but the mapping is a guess you
have to make from names and context, not a given fact). Only report a
current_xpath for a column when you're reasonably confident which candidate
variable corresponds to it; leave it null rather than force a match.

## Raw HTML evidence
{{ evidence }}

For EACH column listed above, report:
- html_value: the value you can read directly from the HTML (or null if the
  HTML doesn't contain that field at all — do not guess). A genuinely absent
  price, a "0", or an availability label that isn't one of the usual
  In Stock / Out of Stock strings are all valid html_values in their own
  right — report exactly what the page shows, do not normalize or discard it.
- real_value: the stored value from the row above, verbatim.
- current_xpath: the xpath the crawler's OWN code currently uses for this
  column, taken verbatim from the candidate xpaths above if you can
  confidently identify which one it is. null if none of the candidates
  plausibly correspond to this column, or no candidates were provided.
- working_xpath: the xpath that actually selects html_value in the HTML
  shown here, derived only from markup you can see. This can be the same
  string as current_xpath (nothing is wrong), or different (that
  difference IS the drift — this is what a fix should use).
- match: true if html_value and real_value represent the same underlying
  fact (allow for type/formatting differences like "194.35" vs 194.35, or
  "4.0" vs 4 — those still match), false if they genuinely differ.

If the HTML is unavailable, set html_value to null and match to false for
every column, and say so — do not fabricate an html_value.


## Output rules (read carefully — these prevent truncation/parse failures)
- Write ONLY the JSON object. Do not reason, explain, or think out loud
  before or after it — any text outside the object will break parsing.
- Every column listed in {{ columns }} must appear exactly once in the
  output array, in the same order given. Never omit a column — if there
  is no evidence for it, still emit it with html_value: null,
  current_xpath: null, working_xpath: null, match: false.
- Keep xpath and value strings as short as possible. Do not include
  reasoning inside any field — only the value itself.
- If a raw xpath contains a double-quote (e.g. //div[@id="price"]),
  escape it as \" — example:
  "working_xpath": "//div[@id=\"price\"]/span"
- Compose the full JSON object mentally before writing any of it, so you
  never start an object you can't finish. If you are running low on
  space, drop detail from string fields (e.g. shorten html_value further)
  rather than truncating the JSON structure itself.
