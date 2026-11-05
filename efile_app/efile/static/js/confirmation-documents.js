(function() {
    const form = document.getElementById("extraction-review-form");
    if (!form) return;
    const context = JSON.parse(document.getElementById("extraction-context").textContent);
    let previous = "";

    function update() {
        const existing = form.querySelector('[name="existing_case"]:checked')?.value === "existing";
        const court = form.elements.court_code.value;
        const category = form.elements.case_category_code.value;
        const type = form.elements.case_type_code.value;
        const key = JSON.stringify([existing, court, category, type]);
        if (key === previous) return;
        previous = key;
        form.querySelectorAll("[data-confirmation-document]").forEach(wrapper => {
            const picker = wrapper.querySelector("[data-document-type-picker]");
            wrapper.hidden = !existing && wrapper.dataset.isSource === "lead";
            if (wrapper.hidden) return;
            if (existing) {
                picker.dataset.choicesUrl = picker.dataset.caseUrl || "";
            } else if (court && category && type) {
                picker.dataset.choicesUrl = `/api/dropdowns/filing-types/?${new URLSearchParams({
                    jurisdiction: context.jurisdiction, court, case_category: category, case_type: type, existing_case: "no"
                })}`;
            } else {
                picker.dataset.choicesUrl = "";
            }
            picker.querySelector("[data-document-type-retry]").hidden = true;
            if (picker.dataset.choicesUrl) window.DocumentTypePicker.load(picker, picker.dataset.choicesUrl);
            else {
                picker.generation = (picker.generation || 0) + 1;
                picker.querySelector("select").disabled = true;
                picker.querySelector("[data-document-type-status]").textContent = existing ?
                    gettext("You can choose this after you find your court case on the next screen.") : gettext("Choose a court and case type first.");
            }
        });
        document.getElementById("confirmation-document-types").hidden =
            Array.from(form.querySelectorAll("[data-confirmation-document]")).every(wrapper => wrapper.hidden);
    }
    form.querySelectorAll("[data-document-type-picker]").forEach(picker => {
        picker.dataset.caseUrl = picker.dataset.choicesUrl;
    });
    form.addEventListener("change", update);
    new MutationObserver(update).observe(document.getElementById("new-case-details"), {
        childList: true,
        subtree: true
    });
    form.addEventListener("submit", event => {
        const loading = Array.from(form.querySelectorAll("[data-confirmation-document]")).some(wrapper =>
            !wrapper.hidden && wrapper.querySelector("[data-document-type-picker]").dataset.choicesUrl && wrapper.querySelector("select").disabled);
        if (loading) {
            event.preventDefault();
            const error = document.getElementById("extraction-review-error");
            error.textContent = gettext("Wait for the document types to load. If they could not load, choose Try again.");
            error.hidden = false;
            error.tabIndex = -1;
            error.focus();
        }
    });
    update();
}());