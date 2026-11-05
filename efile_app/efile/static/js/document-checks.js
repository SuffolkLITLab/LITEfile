/* Confirm or remove a newly prepared copy without leaving the page it was added on.
 *
 * Markup: a [data-document-checks] container (data-url: the document-checks
 * endpoint) holding [data-document-check] blocks. Each finished action fires
 * "document-checks:change" on the container with {action, data}, so the page
 * can update whatever depends on its documents.
 */
const DocumentChecks = {
    pending(root = document) {
        return Boolean(root.querySelector("[data-document-check]"));
    },

    add(container, html) {
        const template = document.createElement("template");
        template.innerHTML = html.trim();
        const check = template.content.firstElementChild;
        container.appendChild(check);
        check.querySelectorAll("[data-pdf-preview]").forEach((details) => window.attachPdfPreview?.(details));
        check.querySelectorAll("[data-document-type-picker]").forEach(picker => window.DocumentTypePicker?.attach(picker));
        return check;
    },

    async submit(button) {
        const container = button.closest("[data-document-checks]");
        const check = button.closest("[data-document-check]");
        const action = button.hasAttribute("data-document-remove") ? "remove" : "confirm";
        const status = check.querySelector("[data-document-check-status]");
        const buttons = check.querySelectorAll("button");
        const type = check.querySelector("[data-document-type-select]");
        if (action === "confirm" && type && (type.disabled || !type.value)) {
            status.textContent = gettext("Choose a type of document before continuing.");
            if (!type.disabled) type.focus();
            return;
        }
        const body = new FormData();
        if (type && action === "confirm") body.append("filing_type", type.value);
        body.append("action", action);
        body.append("document_id", check.dataset.documentId);
        body.append("preview_fingerprint", check.dataset.fingerprint);
        const feeToken = document.getElementById("fee-inputs-token");
        if (feeToken) body.append("fee_inputs_token", JSON.parse(feeToken.textContent));
        body.append("csrfmiddlewaretoken", apiUtils.getCSRFToken());
        buttons.forEach((element) => {
            element.disabled = true;
        });
        status.textContent = action === "remove" ? gettext("Removing document…") : gettext("Saving…");
        try {
            const response = await fetch(window.withFilingDraft(container.dataset.url), {
                method: "POST",
                body,
                credentials: "same-origin"
            });
            const data = await response.json();
            if (!response.ok || !data.success) throw new Error(data.error || gettext("That did not work. Try again."));
            check.remove();
            container.dispatchEvent(new CustomEvent("document-checks:change", {
                bubbles: true,
                detail: {
                    action,
                    data
                }
            }));
        } catch (error) {
            status.textContent = error.message;
            buttons.forEach((element) => {
                element.disabled = false;
            });
        }
    }
};
window.DocumentChecks = DocumentChecks;

document.addEventListener("click", (event) => {
    const button = event.target.closest("[data-document-checks] [data-document-confirm], [data-document-checks] [data-document-remove]");
    if (button) DocumentChecks.submit(button);
});