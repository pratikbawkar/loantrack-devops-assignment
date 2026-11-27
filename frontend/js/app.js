"use strict";

// Relative path on purpose: the browser only talks to the origin that served
// this page (nginx), which proxies /api/* to the backend. No host or port here.
const API_LOANS = "/api/loans";

const FORM_FIELDS = ["borrower_name", "loan_amount", "property_city"];

// Known statuses get a colour; anything else falls back to the neutral badge.
const STATUS_BADGES = {
  PENDING: "badge--pending",
  APPROVED: "badge--approved",
  UNDER_REVIEW: "badge--review",
  REJECTED: "badge--rejected",
  DISBURSED: "badge--disbursed",
};

const MSG_UNREACHABLE = "Cannot reach the LoanTrack service. Check your connection and try again.";
const MSG_UNAVAILABLE = "The loan service is temporarily unavailable (database not reachable). Please try again shortly.";

const amountFormat = new Intl.NumberFormat(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 });
const dateFormat = new Intl.DateTimeFormat(undefined, { dateStyle: "medium", timeStyle: "short" });

const ui = {};

document.addEventListener("DOMContentLoaded", () => {
  ui.form = byId("loan-form");
  ui.submit = byId("submit-button");
  ui.formMessage = byId("form-message");
  ui.refresh = byId("refresh-button");
  ui.loading = byId("loans-loading");
  ui.empty = byId("loans-empty");
  ui.listError = byId("loans-error");
  ui.table = byId("loans-table");
  ui.body = byId("loans-body");
  ui.count = byId("loan-count");
  ui.servedBy = byId("served-by");
  ui.appVersion = byId("app-version");

  ui.form.addEventListener("submit", onSubmit);
  ui.refresh.addEventListener("click", loadLoans);
  loadLoans();
});

function byId(id) {
  return document.getElementById(id);
}

// ---- footer: which backend pod answered, and its version ----

function updateFooter(response) {
  // The backend adds both headers to every response, errors included, so each
  // call shows the pod that served it. If nginx answers by itself (e.g. 502 when
  // no backend is running) they are missing and the last known values stay.
  const servedBy = response.headers.get("X-Served-By");
  const appVersion = response.headers.get("X-App-Version");
  if (servedBy) ui.servedBy.textContent = servedBy;
  if (appVersion) ui.appVersion.textContent = appVersion;
}

// ---- loans table ----

function setListState(state, message) {
  ui.loading.hidden = state !== "loading";
  ui.empty.hidden = state !== "empty";
  ui.listError.hidden = state !== "error";
  ui.listError.textContent = state === "error" ? message : "";
  ui.table.hidden = state !== "ready";
  ui.refresh.disabled = state === "loading";
}

async function loadLoans() {
  setListState("loading");
  ui.count.textContent = "";

  let response;
  try {
    response = await fetch(API_LOANS, { headers: { Accept: "application/json" }, cache: "no-store" });
  } catch {
    setListState("error", MSG_UNREACHABLE);
    return;
  }
  updateFooter(response);

  if (response.status === 503) {
    setListState("error", MSG_UNAVAILABLE);
    return;
  }
  if (!response.ok) {
    setListState("error", `Could not load loans (HTTP ${response.status}).`);
    return;
  }

  const loans = await response.json().catch(() => null);
  if (!Array.isArray(loans)) {
    setListState("error", "Received an unexpected response from the server.");
    return;
  }

  renderLoans(loans);
  ui.count.textContent = `(${loans.length})`;
  setListState(loans.length > 0 ? "ready" : "empty");
}

function renderLoans(loans) {
  ui.body.replaceChildren(...loans.map(buildRow));
}

// Every value from the API goes through textContent, never innerHTML,
// so stored text can never be interpreted as HTML or script.
function buildRow(loan) {
  const row = document.createElement("tr");
  row.append(
    cell(String(loan.id)),
    cell(loan.borrower_name),
    cell(formatAmount(loan.loan_amount), "num"),
    loan.property_city ? cell(loan.property_city) : cell("—", "muted"),
    statusCell(loan.status),
    cell(formatDate(loan.created_at)),
  );
  return row;
}

function cell(text, className) {
  const td = document.createElement("td");
  td.textContent = text;
  if (className) td.className = className;
  return td;
}

function statusCell(status) {
  const badge = document.createElement("span");
  badge.className = "badge";
  if (Object.hasOwn(STATUS_BADGES, status)) badge.classList.add(STATUS_BADGES[status]);
  badge.textContent = status;
  const td = document.createElement("td");
  td.append(badge);
  return td;
}

function formatAmount(value) {
  // The API sends amounts as strings to keep exact decimals; Number is only used
  // for display formatting (14 significant digits fit safely in a double).
  const number = Number(value);
  return Number.isFinite(number) ? amountFormat.format(number) : String(value);
}

function formatDate(value) {
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? String(value) : dateFormat.format(date);
}

// ---- add loan form ----

// Only these three fields are sent; the backend always stores the initial
// state itself and rejects any extra field with 422.
function buildPayload(formData) {
  const city = String(formData.get("property_city") ?? "").trim();
  return {
    borrower_name: String(formData.get("borrower_name") ?? "").trim(),
    loan_amount: String(formData.get("loan_amount") ?? "").trim(),
    property_city: city === "" ? null : city,
  };
}

async function onSubmit(event) {
  event.preventDefault();
  clearFieldErrors();
  hideFormMessage();

  const payload = buildPayload(new FormData(ui.form));
  setSubmitting(true);
  try {
    let response;
    try {
      response = await fetch(API_LOANS, {
        method: "POST",
        headers: { "Content-Type": "application/json", Accept: "application/json" },
        body: JSON.stringify(payload),
      });
    } catch {
      showFormMessage("error", MSG_UNREACHABLE);
      return;
    }
    updateFooter(response);

    if (response.status === 201) {
      const loan = await response.json().catch(() => null);
      ui.form.reset();
      showFormMessage(
        "success",
        loan ? `Loan #${loan.id} for ${loan.borrower_name} added with status ${loan.status}.` : "Loan added.",
      );
      await loadLoans();
      return;
    }
    if (response.status === 422) {
      const body = await response.json().catch(() => null);
      showValidationErrors(body);
      return;
    }
    if (response.status === 503) {
      showFormMessage("error", MSG_UNAVAILABLE);
      return;
    }
    showFormMessage("error", `Could not add the loan (HTTP ${response.status}).`);
  } finally {
    setSubmitting(false);
  }
}

function setSubmitting(submitting) {
  ui.submit.disabled = submitting;
  ui.submit.textContent = submitting ? "Adding…" : "Add loan";
}

function showValidationErrors(body) {
  // FastAPI 422 body: {"detail": [{"loc": ["body", "<field>"], "msg": "..."}]}
  const details = body && Array.isArray(body.detail) ? body.detail : [];
  const general = [];
  for (const error of details) {
    const loc = Array.isArray(error.loc) ? error.loc : [];
    const field = loc[loc.length - 1];
    const message = typeof error.msg === "string" ? error.msg : "Invalid value";
    if (FORM_FIELDS.includes(field)) {
      showFieldError(field, message);
    } else {
      general.push(message);
    }
  }
  const summary = general.length > 0 ? general.join(" ") : "Please correct the highlighted fields.";
  showFormMessage("error", summary);
}

function showFieldError(field, message) {
  const input = byId(field);
  const error = byId(`${field}-error`);
  input.setAttribute("aria-invalid", "true");
  error.textContent = message;
  error.hidden = false;
}

function clearFieldErrors() {
  for (const field of FORM_FIELDS) {
    byId(field).removeAttribute("aria-invalid");
    const error = byId(`${field}-error`);
    error.textContent = "";
    error.hidden = true;
  }
}

function showFormMessage(kind, text) {
  ui.formMessage.className = `message message--${kind}`;
  ui.formMessage.textContent = text;
  ui.formMessage.hidden = false;
}

function hideFormMessage() {
  ui.formMessage.hidden = true;
  ui.formMessage.textContent = "";
}
