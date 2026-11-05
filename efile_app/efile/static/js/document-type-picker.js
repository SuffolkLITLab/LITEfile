/* Document-specific choices; shared by confirmation and in-place PDF checks. */
window.DocumentTypePicker = {
    async load(root, url) {
        const select = root.querySelector("[data-document-type-select]");
        const status = root.querySelector("[data-document-type-status]");
        const retry = root.querySelector("[data-document-type-retry]");
        retry.hidden = true;
        const generation = (root.generation || 0) + 1;
        root.generation = generation;
        const saved = select.value || select.dataset.saved || "";
        select.disabled = true;
        status.textContent = gettext("Loading document types…");
        try {
            const response = await fetch(url);
            const result = await response.json();
            if (!response.ok || !result.success) throw new Error();
            if (generation !== root.generation) return;
            select.replaceChildren(new Option(gettext("Choose a type of document"), ""));
            (result.data || []).forEach(item => select.add(new Option(item.text || item.name, item.value || item.code)));
            select.value = saved;
            select.dataset.saved = "";
            select.disabled = false;
            status.textContent = saved && !select.value ? gettext("The previous type is not available for this case. Choose again.") : "";
        } catch {
            if (generation !== root.generation) return;
            status.textContent = gettext("We could not load document types. Try again.");
            retry.hidden = false;
        }
    },
    attach(root) {
        if (root.dataset.pickerMounted) return;
        root.dataset.pickerMounted = "true";
        root.querySelector("[data-document-type-retry]").addEventListener("click", () => this.load(root, root.dataset.choicesUrl));
        if (root.dataset.choicesUrl) this.load(root, root.dataset.choicesUrl);
    },
};
document.querySelectorAll("[data-document-type-picker]").forEach(root => window.DocumentTypePicker.attach(root));