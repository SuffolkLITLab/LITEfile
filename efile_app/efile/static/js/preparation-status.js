(function() {
    const panel = document.getElementById("preparation-state");
    if (!panel) return;
    const detail = document.getElementById("preparation-detail");
    let attempt = 0;
    async function poll() {
        try {
            const response = await fetch(panel.dataset.statusUrl);
            if (response.redirected) {
                window.location.assign(response.url);
                return;
            }
            const result = await response.json();
            if (!response.ok || !result.success) throw new Error("Unable to check preparation. Your saved uploads are retained; reload to try again.");
            if (!result.preparation || result.preparation.status === "failed") {
                window.location.reload();
                return;
            }
        } catch (error) {
            detail.textContent = error.message;
        }
        // Jitter only spreads status checks; it has no security purpose.
        // eslint-disable-next-line sonarjs/pseudo-random
        window.setTimeout(poll, Math.min(10000, 2500 * 1.5 ** Math.min(attempt++, 4)) * (0.8 + 0.2 * Math.random()));
    }
    panel.querySelectorAll("button[data-action]").forEach(button => {
        button.addEventListener("click", async () => {
            button.disabled = true;
            try {
                const body = new FormData();
                body.append("action", button.dataset.action);
                body.append("csrfmiddlewaretoken", apiUtils.getCSRFToken());
                const response = await fetch(window.location.href, {
                    method: "POST",
                    body
                });
                if (response.redirected) {
                    window.location.assign(response.url);
                    return;
                }
                const result = await response.json();
                if (!response.ok || !result.success) throw new Error(result.error || "Unable to update this upload. Try again.");
                window.location.reload();
            } catch (error) {
                detail.textContent = error.message;
                button.disabled = false;
            }
        });
    });
    if (panel.dataset.status !== "failed") poll();
})();