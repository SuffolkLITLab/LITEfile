(function() {
    function preferredConfidentialityValue(options, savedValue, defaultConfidentiality) {
        if (savedValue) return String(savedValue);
        if (defaultConfidentiality) {
            const preferred = options.find((item) => item.confidentiality === defaultConfidentiality);
            if (preferred) return String(preferred.value || preferred.code || preferred.id || "");
        }
        if (options.length === 1) {
            const only = options[0];
            return String(only.value || only.code || only.id || "");
        }
        return "";
    }

    if (typeof module !== "undefined" && module.exports) {
        module.exports = {
            preferredConfidentialityValue
        };
    }
    if (typeof document === "undefined") return;

    const form = document.getElementById("organize-documents-form");
    const contextElement = document.getElementById("organize-context");
    if (!form || !contextElement) return;

    const context = JSON.parse(contextElement.textContent);
    const list = document.getElementById("organize-list");
    const errorBox = document.getElementById("organize-error");
    const cards = () => Array.from(list.querySelectorAll(".organize-card"));
    // Copy this script renders itself. It comes from the page rather than from
    // string literals here so a state can reword it and a translator can reach
    // it -- see efile/utils/ui_text.py.
    const text = context.text || {};
    let filingTypes = null;
    let filingSuggestion = null;
    const availability = window.filingAvailability.mount({
        form,
        notice: document.getElementById("filing-availability-notice"),
        selection: () => ({
            jurisdiction: context.jurisdiction,
            court: context.court,
            case_category_name: context.case_category_name,
            case_type_name: context.case_type_name,
            filing_type_names: cards().map((card) => {
                const select = card.querySelector(".filing-type");
                // Until the court's list loads, the saved name is the selection.
                if (!card.dataset.filingTypesLoaded) return card.dataset.filingTypeName || "";
                return apiUtils.selectedOptionText(select);
            }),
        }),
    });
    availability.check();

    function optionValue(item) {
        return String(item.value || item.code || item.id || "");
    }

    function optionText(item) {
        return item.text || item.name || item.description || optionValue(item);
    }

    function setOptions(select, options, savedValue, placeholder) {
        select.innerHTML = "";
        if (!options.length) {
            const option = new Option(placeholder, "");
            select.add(option);
            select.disabled = true;
            return;
        }
        select.add(new Option(placeholder, ""));
        options.forEach((item) => {
            const option = new Option(optionText(item), optionValue(item));
            // Some filing types require an amount in controversy; case_questions
            // asks for it later, but only needs to if this is set on the chosen
            // filing type for at least one document in the filing.
            option.dataset.amountInControversyRequired =
                String(item.amountincontroversy || "").toLowerCase() === "required";
            select.add(option);
        });
        select.value = savedValue || "";
        select.disabled = false;
    }

    function isRequiredOption(item) {
        return item.required === true || String(item.required).toLowerCase() === "true";
    }

    function setRadioOptions(
        container,
        options,
        savedValue,
        fieldName,
        placeholder,
        requiredNote,
        defaultConfidentiality,
    ) {
        container.innerHTML = "";
        if (!options.length) {
            setPlaceholder(container, placeholder);
            return;
        }
        const selectedValue = preferredConfidentialityValue(options, savedValue, defaultConfidentiality);
        options.forEach((item) => {
            const label = document.createElement("label");
            const input = document.createElement("input");
            input.className = "form-check-input";
            input.type = "radio";
            input.name = fieldName;
            input.value = optionValue(item);
            input.required = true;
            input.checked = input.value === selectedValue;
            // Carried on the input so the payload can name the choice without
            // scraping whatever else the label happens to show.
            input.dataset.optionText = optionText(item);
            const caption = document.createElement("span");
            caption.textContent = optionText(item);
            // The court will reject a filing that lacks its required component,
            // so say which choice that is instead of offering all of them as
            // though any would do.
            if (requiredNote && isRequiredOption(item)) {
                const note = document.createElement("small");
                note.textContent = requiredNote;
                caption.appendChild(note);
            }
            label.append(input, caption);
            container.appendChild(label);
        });
    }

    // A filing component the court gives no choice about. Presenting the one
    // allowed value as a radio invites the filer to try to clear it -- and the
    // filing is rejected later if they succeed -- so it is shown as what it is:
    // metadata the court fixed, with the value still submitted.
    function setFixedOption(container, item, fieldName) {
        container.innerHTML = "";
        const label = document.createElement("label");
        label.className = "compact-choice-list__fixed";
        const input = document.createElement("input");
        input.type = "radio";
        input.name = fieldName;
        input.value = optionValue(item);
        input.checked = true;
        input.hidden = true;
        input.dataset.optionText = optionText(item);
        const caption = document.createElement("span");
        const name = document.createElement("strong");
        name.textContent = optionText(item);
        caption.appendChild(name);
        if (text.filing_component_fixed_note) {
            const note = document.createElement("small");
            note.textContent = text.filing_component_fixed_note;
            caption.appendChild(note);
        }
        label.append(input, caption);
        container.appendChild(label);
    }

    function setPlaceholder(container, message) {
        container.innerHTML = "";
        const element = document.createElement("small");
        element.textContent = message || "";
        container.appendChild(element);
    }

    function setLoading(container) {
        setPlaceholder(container, text.loading_choices);
    }

    async function getJson(url) {
        const response = await fetch(url, {
            headers: {
                "Content-Type": "application/json",
                "X-CSRFToken": apiUtils.getCSRFToken(),
            },
        });
        const result = await response.json();
        if (!response.ok || !result.success) {
            throw new Error(result.error || "The court's document choices could not be loaded.");
        }
        if (result.suggestion) filingSuggestion = result.suggestion;
        return result.data || [];
    }

    async function loadFilingTypes() {
        if (filingTypes) return filingTypes;
        if (context.existing_case === "yes") {
            filingTypes = await getJson(context.filing_types_url);
            return filingTypes;
        }
        const params = new URLSearchParams({
            jurisdiction: context.jurisdiction,
            court: context.court,
            case_category: context.case_category,
            case_type: context.case_type,
            existing_case: context.existing_case,
            guessed_filing_type: context.guessed_filing_type || "",
        });
        filingTypes = await getJson(`/api/dropdowns/filing-types/?${params}`);
        return filingTypes;
    }

    async function loadDependentOptions(card, filingType) {
        const documentType = card.querySelector(".document-type-options");
        const documentTypeField = documentType.closest("fieldset");
        const component = card.querySelector(".filing-component-options");
        const documentId = card.dataset.documentId;
        if (!filingType) {
            documentTypeField.hidden = false;
            setRadioOptions(documentType, [], "", `document-type-${documentId}`, text.choose_filing_type_first);
            setRadioOptions(component, [], "", `filing-component-${documentId}`, text.choose_filing_type_first);
            return;
        }

        setLoading(documentType);
        setLoading(component);
        const documentParams = new URLSearchParams({
            jurisdiction: context.jurisdiction,
            court: context.court,
            parent: filingType,
        });
        const componentParams = new URLSearchParams({
            jurisdiction: context.jurisdiction,
            court: context.court,
            filing_type: filingType,
        });
        const [documentTypes, components] = await Promise.all([
            getJson(`/api/dropdowns/document-types/?${documentParams}`),
            getJson(`/api/get-filing-components/?${componentParams}`),
        ]);
        documentTypeField.hidden = documentTypes.length === 0;
        setRadioOptions(
            documentType,
            documentTypes,
            card.dataset.documentType,
            `document-type-${documentId}`,
            text.no_document_types,
            undefined,
            context.default_confidentiality,
        );

        let savedComponent = card.dataset.filingComponent;
        if (!savedComponent && components.length) {
            // Each document goes to the court as its own filing, under its own
            // filing type, so each one needs that filing type's *required*
            // component -- the lead document. Defaulting a supporting document
            // to "Attachments" leaves its filing with no lead document at all,
            // which the court rejects ("Required filing component '332' not
            // found") long after the filer has left this screen.
            const preferred = components.find(isRequiredOption) ||
                components.find((item) => String(item.efspcode || "").toUpperCase() === "LEAD");
            savedComponent = optionValue(preferred || components[0]);
        }
        if (components.length === 1) {
            setFixedOption(component, components[0], `filing-component-${documentId}`);
            return;
        }
        setRadioOptions(
            component,
            components,
            savedComponent,
            `filing-component-${documentId}`,
            text.no_filing_components,
            text.filing_component_required_note,
        );
    }

    // Keywords for the optional services filers actually look for. Courts can
    // list a dozen+ services (interpreter requests, sealed filings, various
    // process-server fees...); surfacing all of them by default buries the
    // handful people come here for, so only these -- plus anything marked
    // required -- show before the "Show more options" toggle.
    const COMMON_OPTIONAL_SERVICE_KEYWORDS = [
        "certif",
        "copy",
        "copies",
        "expedit",
        "priority",
        "rush",
        "courtesy",
    ];

    function isCommonOptionalService(name) {
        const lower = (name || "").toLowerCase();
        return COMMON_OPTIONAL_SERVICE_KEYWORDS.some((keyword) => lower.includes(keyword));
    }

    function buildOptionalServiceLabel(service, saved) {
        const code = String(service.code ?? service.id ?? "");
        const label = document.createElement("label");
        label.className = "form-check mb-2";
        const input = document.createElement("input");
        input.type = "checkbox";
        input.className = "form-check-input optional-service";
        input.value = code;
        input.checked = Boolean(service.required) || saved.has(code);
        input.disabled = Boolean(service.required);
        const span = document.createElement("span");
        span.className = "form-check-label";
        let text = service.name || service.text || code;
        const fee = parseFloat(service.fee);
        if (fee > 0) text += ` ($${fee.toFixed(2)})`;
        span.textContent = text;
        label.append(input, span);
        return {
            code,
            label
        };
    }

    function appendOptionalService(container, service, saved) {
        const {
            code,
            label
        } = buildOptionalServiceLabel(service, saved);
        if (!code) return;
        container.appendChild(label);
        if (service.description) {
            const description = document.createElement("small");
            description.className = "d-block optional-service-description";
            description.textContent = service.description;
            container.appendChild(description);
        }
    }

    async function loadOptionalServices(card, filingType) {
        const container = card.querySelector(".optional-services-list");
        if (!filingType) {
            setPlaceholder(container, text.choose_filing_type_first);
            return;
        }
        setLoading(container);
        const params = new URLSearchParams({
            jurisdiction: context.jurisdiction,
            court: context.court,
            filing_type_id: filingType,
        });
        let services;
        try {
            services = await getJson(`/api/dropdowns/optional-services/?${params}`);
        } catch {
            container.innerHTML = "";
            return;
        }
        services = services.filter((service) => service.code ?? service.id);
        if (!services.length) {
            container.innerHTML = "";
            return;
        }

        const saved = new Set((card.dataset.optionalServices || "").split(",").filter(Boolean));
        let primary = services.filter((service) => service.required || isCommonOptionalService(service.name));
        let rest = services.filter((service) => !primary.includes(service));
        if (!primary.length) {
            primary = services.slice(0, 4);
            rest = services.slice(4);
        }

        container.innerHTML = "";
        primary.forEach((service) => appendOptionalService(container, service, saved));

        if (rest.length) {
            const moreContainer = document.createElement("div");
            moreContainer.hidden = true;
            rest.forEach((service) => appendOptionalService(moreContainer, service, saved));

            const toggle = document.createElement("button");
            toggle.type = "button";
            toggle.className = "btn btn-link optional-services-toggle";
            toggle.setAttribute("aria-expanded", "false");
            const showMoreText = interpolate(ngettext("Show %s more option", "Show %s more options", rest.length), [rest.length]);
            toggle.textContent = showMoreText;
            toggle.addEventListener("click", () => {
                const wasExpanded = !moreContainer.hidden;
                moreContainer.hidden = wasExpanded;
                toggle.setAttribute("aria-expanded", String(!wasExpanded));
                toggle.textContent = wasExpanded ? showMoreText : gettext("Show fewer options");
            });

            container.append(toggle, moreContainer);
        }
    }

    async function initializeCard(card) {
        const filingType = card.querySelector(".filing-type");
        const options = await loadFilingTypes();
        let selected = card.dataset.filingType;
        const suggestionMessage = card.querySelector(".filing-suggestion-message");
        if (suggestionMessage && !selected && filingSuggestion && filingSuggestion.status !== "none") {
            suggestionMessage.hidden = false;
            if (filingSuggestion.status === "matched") {
                selected = filingSuggestion.value;
                suggestionMessage.textContent = interpolate(gettext("Suggested filing code: %(name)s. Change it if needed."), {
                    name: filingSuggestion.name
                }, true);
            } else {
                suggestionMessage.textContent = interpolate(gettext("“%(name)s” is not on the court’s list. Choose a type below or use Find my filing code."), {
                    name: filingSuggestion.name
                }, true);
            }
        }
        setOptions(filingType, options, selected, "Choose a filing code");
        card.dataset.filingTypesLoaded = "true";
        if (new URLSearchParams(window.location.search).get("find_filing_type") === "1" && card === cards()[0]) {
            const finder = card.querySelector(".filing-type-finder");
            if (finder) finder.open = true;
        }
        window.filingCodeSearch?.mountFilingTypeOnly({
            container: card.querySelector(".filing-type-finder"),
            select: filingType,
            choices: filingTypes
        });
        filingType.addEventListener("change", async () => {
            if (suggestionMessage && filingType.value) suggestionMessage.hidden = true;
            availability.check(filingType.closest(".form-field") || filingType.parentElement);
            card.dataset.documentType = "";
            card.dataset.filingComponent = "";
            try {
                await Promise.all([
                    loadDependentOptions(card, filingType.value),
                    loadOptionalServices(card, filingType.value),
                ]);
            } catch (error) {
                showError(error.message);
            }
        });
        await Promise.all([
            loadDependentOptions(card, filingType.value),
            loadOptionalServices(card, filingType.value),
        ]);

        const checkbox = card.querySelector(".courtesy-copy-toggle");
        const emailWrap = card.querySelector(".courtesy-email-wrap");
        const email = card.querySelector(".courtesy-email");
        checkbox.addEventListener("change", () => {
            emailWrap.hidden = !checkbox.checked;
            email.required = checkbox.checked;
            if (!checkbox.checked) email.value = "";
        });
        email.required = checkbox.checked;
    }

    function selectedMainDocumentId() {
        const field = form.elements.namedItem("main_document");
        const documentId = Number(field?.value);
        return Number.isInteger(documentId) && documentId > 0 ? documentId : null;
    }

    function updatePositions() {
        const supporting = cards().filter((card) => card.dataset.role === "supporting");
        supporting.forEach((card, index) => {
            card.querySelector(".organize-card__position").textContent = `Additional document ${index + 1}`;
            card.querySelector(".move-up").disabled = index === 0;
            card.querySelector(".move-down").disabled = index === supporting.length - 1;
        });
    }

    list.addEventListener("click", (event) => {
        const button = event.target.closest(".move-up, .move-down");
        if (!button) return;
        const card = button.closest(".organize-card");
        const supporting = cards().filter((item) => item.dataset.role === "supporting");
        const index = supporting.indexOf(card);
        if (button.classList.contains("move-up") && index > 0) {
            list.insertBefore(card, supporting[index - 1]);
        } else if (button.classList.contains("move-down") && index < supporting.length - 1) {
            supporting[index + 1].after(card);
        }
        updatePositions();
    });

    function showError(message) {
        errorBox.textContent = message;
        errorBox.hidden = false;
        errorBox.scrollIntoView({
            behavior: "smooth",
            block: "center"
        });
    }

    form.addEventListener("submit", async (event) => {
        event.preventDefault();
        errorBox.hidden = true;
        if (!form.reportValidity()) return;
        const button = document.getElementById("save-document-details");
        button.disabled = true;
        const documents = cards().map((card) => {
            const filingType = card.querySelector(".filing-type");
            const documentType = card.querySelector('.document-type-options input:checked');
            const component = card.querySelector('.filing-component-options input:checked');
            const courtesyEmail = card.querySelector(".courtesy-copy-toggle").checked ?
                card.querySelector(".courtesy-email").value : "";
            const requestedOptionalServices = Array.from(card.querySelectorAll(".optional-service:checked"))
                .map((input) => input.value);
            return {
                id: Number(card.dataset.documentId),
                name: card.querySelector(".document-name").value,
                filing_type: filingType.value,
                filing_type_name: apiUtils.cleanOptionText(filingType.selectedOptions[0]?.text),
                document_type: documentType?.value || "",
                document_type_name: documentType?.dataset.optionText || "",
                filing_component: component?.value || "",
                filing_component_name: component?.dataset.optionText || "",
                courtesy_copy_email: courtesyEmail,
                requested_optional_services: requestedOptionalServices,
                requires_amount_in_controversy: filingType.selectedOptions[0]?.dataset.amountInControversyRequired === "true",
            };
        });

        try {
            const response = await fetch(window.location.href, {
                method: "POST",
                headers: {
                    "Content-Type": "application/json",
                    "X-CSRFToken": apiUtils.getCSRFToken(),
                },
                body: JSON.stringify({
                    documents,
                    case_fingerprint: context.case_fingerprint,
                    // Null when nothing is selected. The server then falls back to
                    // the draft's own lead document; picking a card here as well
                    // would be a second, separately-written answer to the same
                    // question, free to disagree with the first one silently.
                    main_document_id: selectedMainDocumentId(),
                    return_to: context.return_to || "",
                }),
            });
            if (response.redirected) {
                window.location.assign(response.url);
                return;
            }
            const result = await response.json();
            if (!response.ok || !result.success) throw new Error(result.error || "Could not save document details.");
            window.location.assign(result.redirect_url);
        } catch (error) {
            showError(error.message);
            button.disabled = false;
        }
    });

    Promise.all(cards().map(initializeCard))
        .then(() => {
            updatePositions();
            availability.check();
        })
        .catch((error) => showError(error.message));
})();