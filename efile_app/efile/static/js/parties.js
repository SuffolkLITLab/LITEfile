(function() {
    const guessButton = document.getElementById("apply-party-type-guess");
    const roleRadios = Array.from(document.querySelectorAll('input[name="filer_party_type"]'));
    const notAParty = document.getElementById("filer-not-a-party");
    const filingFor = document.getElementById("filing-for");

    function selectRole(value) {
        const radio = roleRadios.find((input) => input.value === value);
        if (!radio) return;
        radio.checked = true;
        radio.dispatchEvent(new Event("change", {
            bubbles: true
        }));
        radio.scrollIntoView({
            behavior: "smooth",
            block: "center"
        });
        radio.focus();
    }

    if (guessButton) {
        guessButton.addEventListener("click", () => {
            const hint = document.getElementById("party-type-hint");
            if (hint) hint.hidden = true;
            selectRole(guessButton.dataset.value);
            if (roleForm && saveRole) roleForm.requestSubmit(saveRole);
        });
    }

    // "Who are you filing for?" only means anything to someone who has said
    // they are not a party themselves, so it follows that answer rather than
    // sitting on the screen as a second unexplained question.
    if (notAParty && filingFor) {
        const syncFilingFor = () => {
            filingFor.hidden = !notAParty.checked;
            filingFor.disabled = !notAParty.checked;
        };
        roleRadios.forEach((radio) => radio.addEventListener("change", syncFilingFor));
        syncFilingFor();
    }

    // Continue carries whichever role is selected, so what it says it will do
    // next follows the selection rather than the role last saved.
    const previewsEl = document.getElementById("continue-previews");
    const continueLabel = document.getElementById("continue-from-parties-label");
    const continueButton = document.getElementById("continue-from-parties");
    const continueHint = document.getElementById("party-list-next");
    if (previewsEl && continueLabel && continueButton && continueHint) {
        const previews = JSON.parse(previewsEl.textContent);
        const syncContinue = () => {
            const selected = roleRadios.find((input) => input.checked);
            const preview = previews[selected ? selected.value : ""] || previews[""];
            continueLabel.textContent = preview.label;
            continueHint.textContent = preview.hint;
            continueHint.hidden = !preview.hint;
            if (preview.hint) continueButton.setAttribute("aria-describedby", continueHint.id);
            else continueButton.removeAttribute("aria-describedby");
        };
        roleRadios.forEach((radio) => radio.addEventListener("change", syncContinue));
        syncContinue();
    }

    // Pointer selection saves immediately. Native radio arrow keys also fire
    // change (and keyboard clicks have detail 0), so keep keyboard selection
    // on this page until the person chooses Save role or Continue.
    const roleForm = document.getElementById("your-role");
    const saveRole = roleForm?.querySelector('button[value="save_role"]');
    if (roleForm && saveRole) {
        roleRadios.forEach((radio) => radio.addEventListener("click", (event) => {
            if (event.detail > 0 && radio.checked && radio !== notAParty) roleForm.requestSubmit(saveRole);
        }));
    }

    // The party list's "Add me as a party" shortcut: the filer is already on
    // this draft with a name and address, so adding themselves is answering
    // the role question above, not typing themselves in again. It takes them
    // to the question rather than answering it -- which party type they are
    // is a legal question, and picking one for them is how a filer ends up
    // filed under the wrong role without ever reading it.
    const addMe = document.getElementById("add-me-as-party");
    if (addMe) {
        addMe.addEventListener("click", () => {
            const firstPartyType = roleRadios.find((input) => input !== notAParty);
            if (!firstPartyType) return;
            if (notAParty && notAParty.checked) {
                notAParty.checked = false;
                // Nothing is selected now, and what Continue says follows that.
                notAParty.dispatchEvent(new Event("change", {
                    bubbles: true
                }));
            }
            if (filingFor) filingFor.hidden = true;
            firstPartyType.scrollIntoView({
                behavior: "smooth",
                block: "center"
            });
            firstPartyType.focus();
        });
    }
})();