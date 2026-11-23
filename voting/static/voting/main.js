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
          window.location.replace(redemption.dataset.ballot);
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
    const button = form.querySelector("button");
    const status = form.querySelector("[role='status']");
    const input = form.querySelector("input[type='text']");
    button.disabled = true;
    input.value = "";
    input.hidden = true;
    status.textContent = "";
    status.hidden = true;
    try {
      const response = await fetch(form.action, { method: "POST", body: new FormData(form), cache: "no-store" });
      const result = await response.json();
      status.hidden = false;
      if (!response.ok) {
        status.textContent = result.error || "Impossibile recuperare il link.";
      } else {
        try {
          await navigator.clipboard.writeText(result.url);
          status.textContent = "Link copiato.";
        } catch {
          input.hidden = false;
          input.value = result.url;
          input.focus();
          input.select();
          status.textContent = "Copia il link selezionato.";
        }
      }
    } catch {
      status.hidden = false;
      status.textContent = "Impossibile recuperare il link. Ricarica la pagina e riprova.";
    } finally {
      button.disabled = false;
    }
  });
}
