/* Shared SQL-error helpers — loaded before app.js / race.js */
"use strict";

// SQL errors carry their error class inline, e.g. "[DIVIDE_BY_ZERO] Division by zero…".
const errClass = (msg) => ((msg || "").match(/^\s*\[([A-Z0-9_.]+)\]/) || [])[1] || null;
const errMsg = (msg) => (msg || "").replace(/^\s*\[[A-Z0-9_.]+\]\s*/, "");

const ERROR_HINTS = {
  DIVIDE_BY_ZERO: "The query divides by a value that is 0 for some row — engines differ in ANSI strictness, so one may raise an error where the other returns NULL. Portable fix in the dashboard SQL: try_divide() or NULLIF().",
  INVALID_EXTRACT_BASE_FIELD_TYPE: "This dataset uses a dashboard parameter in a form the race can't emulate yet (e.g. a date-range parameter read as :param.min/:param.max), so the substituted SQL is invalid. This is a race-harness limitation, not a warehouse problem.",
};

function explainError(reyErr, baseErr) {
  const parts = [];
  if (reyErr && baseErr) parts.push("Both lanes failed, so the cause is in the query or its parameters rather than an engine difference.");
  else parts.push(`Only the ${reyErr ? "Reyden" : "baseline"} lane failed — the two engines treat this SQL differently.`);
  const cls = errClass((reyErr || baseErr).error);
  if (cls && ERROR_HINTS[cls]) parts.push(ERROR_HINTS[cls]);
  return parts.join(" ");
}
