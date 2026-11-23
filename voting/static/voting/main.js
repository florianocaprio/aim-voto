"use strict";

const administratorScope = document.querySelector("[data-administrator-scope]");
if (administratorScope) {
  const scope = administratorScope.elements.scope;
  const assignments = administratorScope.querySelector("[data-assigned-elections]");
  const showAssignments = () => {
    assignments.hidden = scope.value === "global";
    for (const checkbox of assignments.querySelectorAll("input[type=checkbox]")) {
      checkbox.disabled = assignments.hidden;
    }
  };
  scope.addEventListener("change", showAssignments);
  showAssignments();
}

const questionRules = document.querySelector("[data-question-rules]");
if (questionRules) {
  const { deliberative_majority: majority, majority_override: fraction, majority_basis: basis,
    deliberation_type: matter, kind } = questionRules.elements;
  const presets = JSON.parse(document.getElementById("question-presets").textContent);
  const updateRuleFields = () => {
    const isMotion = (kind.value === "motion" || kind.value === "motion_req") && matter?.value !== "advisory";
    const isQualified = isMotion && majority.value === "QUALIFIED_MAJORITY";
    for (const [field, visible] of [[majority, isMotion], [fraction, isQualified], [basis, isQualified]]) {
      field.parentElement.hidden = !visible && field.getAttribute("aria-invalid") !== "true";
    }
  };
  matter?.addEventListener("change", () => {
    const preset = presets[matter.value || "ordinary"];
    if (preset) [majority.value, fraction.value, basis.value] = preset;
    updateRuleFields();
  });
  kind.addEventListener("change", updateRuleFields);
  majority.addEventListener("change", updateRuleFields);
  updateRuleFields();
}

const redemption = document.querySelector("[data-redeem]");

if (redemption) {
  const status = redemption.querySelector("[data-redeem-status]");
  const token = window.location.hash.slice(1);
  history.replaceState(null, "", window.location.pathname);
  if (!token) {
    status.textContent = "Riapri il link personale ricevuto dall’associazione.";
  } else {
    const body = new FormData(redemption.querySelector("form"));
    body.set("token", token);
    fetch(window.location.pathname, { method: "POST", body, cache: "no-store" })
      .then(async (response) => {
        const result = await response.json();
        if (response.ok) {
          window.location.replace(result.ballot_url || redemption.dataset.ballot);
        } else {
          status.textContent = result.error || "Apri di nuovo il link personale.";
        }
      })
      .catch(() => {
        status.textContent = "Impossibile aprire la scheda. Riprova dal link ricevuto.";
      });
  }
}

for (const form of document.querySelectorAll("[data-copy-link]")) {
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    const buttons = form.querySelectorAll("button");
    const submitter = event.submitter;
    const status = form.querySelector("[role='status']");
    const input = form.querySelector("[data-copy-value]");
    const chat = form.querySelector("[data-whatsapp-chat]");
    const body = new FormData(form);
    if (submitter?.name === "whatsapp_phone") {
      body.set(submitter.name, submitter.value);
    }
    for (const button of buttons) button.disabled = true;
    input.value = "";
    input.hidden = true;
    status.textContent = "";
    status.hidden = true;
    if (chat) {
      chat.hidden = true;
      chat.removeAttribute("href");
    }
    try {
      const response = await fetch(form.action, { method: "POST", body, cache: "no-store" });
      const result = await response.json();
      status.hidden = false;
      if (!response.ok) {
        status.textContent = result.error || "Impossibile recuperare il link.";
        return;
      }
      const forWhatsApp = chat && result.whatsapp_url;
      const text = forWhatsApp
        ? "Ciao, ecco il tuo link personale per votare in AIM Voto:\n" + result.url
        : result.url;
      if (forWhatsApp) {
        chat.href = result.whatsapp_url;
        chat.hidden = false;
      }
      try {
        await navigator.clipboard.writeText(text);
      } catch {
        input.hidden = false;
        input.value = text;
        input.focus();
        input.select();
        status.textContent = forWhatsApp
          ? "Copia il messaggio selezionato, apri la chat e incollalo."
          : "Copia il link selezionato.";
        return;
      }
      status.textContent = forWhatsApp
        ? "Messaggio copiato. Incollalo nella chat WhatsApp e premi Invia."
        : "Link copiato.";
      if (forWhatsApp) {
        window.open(chat.href, "_blank", "noopener,noreferrer");
      }
    } catch {
      status.hidden = false;
      status.textContent = "Impossibile recuperare il link. Ricarica la pagina e riprova.";
    } finally {
      for (const button of buttons) button.disabled = false;
    }
  });
}

const delegateSearch = document.querySelector("[data-delegate-search]");
if (delegateSearch) {
  const form = delegateSearch.querySelector("form");
  const input = form.elements.q;
  const results = delegateSearch.querySelector("[data-delegate-results]");
  const status = delegateSearch.querySelector("[role='status']");
  let timer;
  let pending;

  async function search(query) {
    const controller = new AbortController();
    pending = controller;
    const url = new URL(form.action);
    url.search = new URLSearchParams({ q: query, format: "json" });
    status.textContent = "Ricerca in corso…";
    try {
      const response = await fetch(url, { cache: "no-store", signal: controller.signal });
      const data = await response.json();
      if (controller.signal.aborted) return;
      if (!response.ok) {
        status.textContent = data.error || "Ricerca non disponibile.";
        return;
      }
      for (const candidate of data.candidates) {
        const item = document.createElement("li");
        const label = document.createElement(candidate.url ? "a" : "span");
        label.textContent = candidate.first_name + " " + candidate.last_name + " · " + candidate.external_id;
        if (candidate.url) label.href = candidate.url;
        item.append(label);
        if (candidate.unavailable) {
          const notice = document.createElement("small");
          notice.textContent = candidate.unavailable;
          item.append(notice);
        }
        results.append(item);
      }
      results.hidden = data.candidates.length === 0;
      status.textContent = data.candidates.length
        ? (data.candidates.some((candidate) => candidate.url) ? "Seleziona il socio." : "Cerca un altro socio.")
        : "Nessun risultato. Prova con un altro nome o matricola.";
    } catch {
      if (!controller.signal.aborted) status.textContent = "Ricerca non disponibile. Riprova.";
    }
  }

  function startSearch(delay) {
    clearTimeout(timer);
    pending?.abort();
    results.replaceChildren();
    results.hidden = true;
    const query = input.value.trim();
    if (query.length < 2) {
      status.textContent = query ? "Inserisci almeno due caratteri." : "";
      return;
    }
    status.textContent = "";
    timer = setTimeout(() => search(query), delay);
  }

  input.addEventListener("input", () => startSearch(250));
  form.addEventListener("submit", (event) => {
    event.preventDefault();
    startSearch(0);
  });
}
