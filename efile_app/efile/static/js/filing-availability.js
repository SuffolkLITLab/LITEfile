(function() {
    function mount({
        form,
        notice,
        selection
    }) {
        let generation = 0;
        let blocked = true;
        let controller;
        const buttons = Array.from(form.querySelectorAll('button[type="submit"]'));
        // Only the buttons this guard disabled are its to re-enable; a page
        // disables its own while a save or lookup is in flight.
        const held = new Set();

        function show(message, unavailable) {
            blocked = unavailable;
            notice.textContent = message;
            notice.hidden = !message;
            buttons.forEach((button) => {
                if (unavailable && !button.disabled) {
                    button.disabled = true;
                    held.add(button);
                } else if (!unavailable && held.has(button)) {
                    button.disabled = false;
                }
            });
            if (!unavailable) held.clear();
        }

        let queued = false;
        let pendingAnchor = null;
        let lastQuery = null;

        // Pages call this from every step of a cascade. Calls made in the same
        // turn collapse into one check, and an unchanged selection is not re-sent.
        function check(anchor) {
            if (anchor) pendingAnchor = anchor;
            if (queued) return;
            queued = true;
            queueMicrotask(run);
        }

        async function run() {
            queued = false;
            if (pendingAnchor) pendingAnchor.append(notice);
            pendingAnchor = null;
            const values = selection();
            const params = new URLSearchParams({
                jurisdiction: values.jurisdiction,
                court: values.court || "",
                case_category_name: values.case_category_name || "",
                case_type_name: values.case_type_name || "",
            });
            (values.filing_type_names || []).forEach((value) => params.append("filing_type_name", value));
            const query = params.toString();
            if (query === lastQuery) return;
            lastQuery = query;
            const token = ++generation;
            controller?.abort();
            if (!values.court) {
                show("", false);
                return;
            }
            controller = new AbortController();
            show(gettext("Checking filing availability…"), true);
            try {
                const response = await fetch(`/api/filing-availability/?${query}`, {
                    signal: controller.signal
                });
                const result = await response.json();
                if (!response.ok || !result.success) throw new Error("Availability check failed");
                if (token !== generation) return;
                show(result.message, !result.available);
            } catch (error) {
                if (token !== generation || error.name === "AbortError") return;
                lastQuery = null;
                show(gettext("We could not check filing availability. Reload this page to try again."), true);
            }
        }

        // Also guard Enter-key submission while a check is pending or blocked.
        form.addEventListener("submit", (event) => {
            if (!blocked) return;
            event.preventDefault();
            event.stopImmediatePropagation();
            notice.focus();
        }, true);
        return {
            check,
            canSubmit: () => !queued && !blocked
        };
    }
    window.filingAvailability = {
        mount
    };
})();