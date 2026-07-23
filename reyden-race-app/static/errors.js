/* Shared SQL-error helpers — loaded before app.js / race.js */
"use strict";

// SQL errors carry their error class inline, e.g. "[DIVIDE_BY_ZERO] Division by zero…".
const errClass = (msg) => ((msg || "").match(/^\s*\[([A-Z0-9_.]+)\]/) || [])[1] || null;
const errMsg = (msg) => (msg || "").replace(/^\s*\[[A-Z0-9_.]+\]\s*/, "");

const ERROR_HINTS = {
  DIVIDE_BY_ZERO: "The query divides by a value that is 0 for some row — engines differ in ANSI strictness, so one may raise an error where the other returns NULL. Portable fix in the dashboard SQL: try_divide() or NULLIF().",
  INVALID_EXTRACT_BASE_FIELD_TYPE: "The dashboard-parameter substitution produced a value this expression can't operate on — the substituted SQL is invalid, not the warehouse. Check the dashboard parameter's default value; it likely isn't a valid date/time for how the query uses it.",
  UNBOUND_SQL_PARAMETER: "This dataset references a dashboard parameter that has no default value, so the race leaves it unsubstituted. The dashboard itself couldn't run this dataset without user input either.",
};

function explainError(reyErr, baseErr) {
  const parts = [];
  if (reyErr && baseErr) parts.push("Both lanes failed, so the cause is in the query or its parameters rather than an engine difference.");
  else parts.push(`Only the ${reyErr ? "Reyden" : "baseline"} lane failed — the two engines treat this SQL differently.`);
  const cls = errClass((reyErr || baseErr).error);
  if (cls && ERROR_HINTS[cls]) parts.push(ERROR_HINTS[cls]);
  return parts.join(" ");
}
