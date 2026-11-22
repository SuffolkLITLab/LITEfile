(function() {
    const panel = document.getElementById("preparation-state");
    if (!panel) return;
    panel.scrollIntoView({
        block: "nearest"
    });
    const detail = document.getElementById("preparation-detail");
    const initialDetail = detail.textContent.trim();
    let attempt = 0;
    async function poll() {
        try {
            const result = await documentRequests.status(
                panel.dataset.statusUrl,
                "We could not check file preparation. We’ll try again shortly."
            );
            if (!result.preparation || result.preparation.status === "failed") {
                window.location.reload();
                return;
            }
            detail.textContent = result.preparation.delay_message || initialDetail;
        } catch (error) {
            detail.textContent = error.message;
            if (error.kind !== "uncertain") return;
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
                body.append("preparation_job_id", panel.dataset.jobId);
                body.append("csrfmiddlewaretoken", apiUtils.getCSRFToken());
                await documentRequests.json(
                    window.location.href,
                    "We could not confirm the change. Reload this page to check your upload.", {
                        method: "POST",
                        body
                    }
                );
                window.location.reload();
            } catch (error) {
                detail.textContent = error.message;
                button.disabled = error.kind !== "rejected";
            }
        });
    });
    if (panel.dataset.status !== "failed") poll();
})();