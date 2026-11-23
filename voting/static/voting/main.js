"use strict";

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
        const link = document.createElement("a");
        link.href = candidate.url;
        link.textContent = candidate.first_name + " " + candidate.last_name + " · " + candidate.external_id;
        item.append(link);
        results.append(item);
      }
      results.hidden = data.candidates.length === 0;
      status.textContent = data.candidates.length
        ? "Seleziona il socio."
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
