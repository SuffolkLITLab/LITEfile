(function() {
    const contextEl = document.getElementById("extraction-context");
    const form = document.getElementById("extraction-review-form");
    if (!contextEl || !form) return;

    const context = JSON.parse(contextEl.textContent);
    const guesses = context.guesses || {};

    // "new", "existing", or "" before the filer has answered.
    function chosenPath() {
        return form.querySelector('input[name="existing_case"]:checked')?.value || "";
    }

    function isExisting() {
        return chosenPath() === "existing";
    }
    const errorBox = document.getElementById("extraction-review-error");
    const acknowledgement = document.getElementById("reviewed_extraction");
    const acknowledgementError = document.getElementById("reviewed-extraction-error");

    const statusEl = document.getElementById("confirm-case-status");

    const fields = {
        court: {
            select: document.getElementById("court_code"),
            nameInput: document.getElementById("court_name"),
            guessKey: "court",
            current: {
                code: context.court_code || "",
                text: context.court_name || ""
            },
        },
        case_category: {
            select: document.getElementById("case_category_code"),
            nameInput: document.getElementById("case_category_name"),
            guessKey: "case category",
            current: {
                code: context.case_category_code || "",
                text: context.case_category_name || ""
            },
        },
        case_type: {
            select: document.getElementById("case_type_code"),
            nameInput: document.getElementById("case_type_name"),
            guessKey: "case type",
            current: {
                code: context.case_type_code || "",
                text: context.case_type_name || ""
            },
        },
        filing_type: {
            select: document.getElementById("filing_type_code"),
            nameInput: document.getElementById("filing_type_name"),
            guessKey: "filing type",
            current: {
                code: context.filing_type_code || "",
                text: context.filing_type_name || ""
            },
        },
    };

    const availability = window.filingAvailability.mount({
        form,
        notice: document.getElementById("filing-availability-notice"),
        selection: () => isExisting() ? ({
            jurisdiction: context.jurisdiction
        }) : ({
            jurisdiction: context.jurisdiction,
            court: fields.court.select.value,
            case_category_name: apiUtils.selectedOptionText(fields.case_category.select),
            case_type_name: apiUtils.selectedOptionText(fields.case_type.select),
            filing_type_names: [apiUtils.selectedOptionText(fields.filing_type.select)],
        }),
    });

    const ORDER = ["court", "case_category", "case_type", "filing_type"];

    const DOWNSTREAM = {
        court: ["case_category", "case_type", "filing_type"],
        case_category: ["case_type", "filing_type"],
        case_type: ["filing_type"],
        filing_type: [],
    };

    const PARENT = {
        case_category: "court",
        case_type: "case_category",
        filing_type: "case_type",
    };

    const PLACEHOLDERS = {
        court: "Choose a court",
        case_category: "Choose a court first",
        case_type: "Choose a case category first",
        filing_type: "Choose a case type first",
    };

    function announce(message) {
        if (!statusEl) return;
        // Cleared first so saying the same thing twice is still said.
        statusEl.textContent = "";
        window.setTimeout(() => {
            statusEl.textContent = message;
        }, 50);
    }

    // Each field's state lives here, apart from which of its two panels is
    // showing. `current` is the filer's choice, kept while its list reloads so
    // a change further up keeps it wherever it is still offered. `mode` is
    // "found" (a summary with Edit) or "edit" (the dropdown), and only the
    // filer's own Edit, Update and Cancel -- or a choice that stopped being
    // valid -- move it. Loading a list never does.
    Object.entries(fields).forEach(([key, field]) => {
        const root = field.select.closest(".review-field");
        field.root = root;
        field.display = root.querySelector(".review-field__display");
        field.input = root.querySelector(".review-field__input");
        field.valueEl = root.querySelector(".review-field__value");
        field.hint = root.querySelector(".review-field__hint");
        field.defaultHint = field.hint.textContent.trim();
        field.editButton = root.querySelector(".review-field__edit");
        const heading = root.querySelector(":scope > label, :scope > span");
        field.label = (heading?.firstChild?.textContent || key).trim();
        field.mode = field.input.hidden ? "found" : "edit";
        field.loading = false;
        field.loaded = false;
        if (field.display) {
            field.checking = document.createElement("small");
            field.checking.className = "review-field__checking";
            field.checking.hidden = true;
            field.display.querySelector(".review-field__found").appendChild(field.checking);
        }
        field.editButton?.addEventListener("click", () => beginEdit(key));
    });

    function setMode(key, mode) {
        const field = fields[key];
        field.mode = mode;
        if (field.display) field.display.hidden = mode !== "found";
        field.input.hidden = mode !== "edit";
    }

    function optionValue(item) {
        return String(item.value ?? item.code ?? item.id ?? "");
    }

    function escapeHtml(value) {
        const holder = document.createElement("span");
        holder.textContent = value ?? "";
        return holder.innerHTML;
    }

    function optionText(item) {
        return item.text || item.name || optionValue(item);
    }

    async function getJson(url, signal) {
        const response = await fetch(url, {
            headers: {
                "X-CSRFToken": apiUtils.getCSRFToken()
            },
            signal,
        });
        const result = await response.json();
        if (!response.ok || !result.success) {
            throw new Error(result.error || "Could not load choices from the court.");
        }
        return result.data || [];
    }

    // One number per field, raised whenever its list -- or any list above it
    // -- is asked for again. A response that comes back carrying an old number
    // is for a choice the filer has since moved away from, and is dropped
    // rather than allowed to replace the list for the current one.
    const generations = {
        court: 0,
        case_category: 0,
        case_type: 0,
        filing_type: 0
    };
    const inFlight = {};

    function supersede(key) {
        [key, ...DOWNSTREAM[key]].forEach((name) => {
            generations[name] += 1;
            if (inFlight[name]) inFlight[name].abort();
            delete inFlight[name];
        });
        return generations[key];
    }

    function isCurrent(key, token) {
        return generations[key] === token;
    }

    function remember(key) {
        const field = fields[key];
        const option = field.select.selectedOptions[0];
        const code = field.select.value;
        const text = apiUtils.selectedOptionText(field.select);
        field.current = {
            code,
            text
        };
        field.nameInput.value = text;
        if (field.valueEl && code) {
            field.valueEl.textContent = text + (option.textContent.trim().endsWith("*") ? " *" : "");
        }
    }

    function startLoading(key, message) {
        const field = fields[key];
        field.loading = true;
        field.select.disabled = true;
        field.select.innerHTML = `<option value="">${escapeHtml(message)}</option>`;
        field.root.setAttribute("aria-busy", "true");
        availability.check();
        if (field.checking) {
            field.checking.textContent = gettext("Checking this is still offered…");
            field.checking.hidden = false;
        }
    }

    function finishLoading(key) {
        const field = fields[key];
        field.loading = false;
        field.root.removeAttribute("aria-busy");
        if (field.checking) field.checking.hidden = true;
    }

    function clearField(key, placeholder) {
        // Nothing above this field to choose from yet. Its own choice is kept
        // in `current`, so putting the parent back puts it back too.
        const field = fields[key];
        finishLoading(key);
        field.select.innerHTML = `<option value="">${escapeHtml(placeholder)}</option>`;
        field.select.disabled = true;
        field.nameInput.value = "";
        field.hint.textContent = field.defaultHint;
        setMode(key, "edit");
        availability.check();
    }

    function failField(key, message, placeholder) {
        const field = fields[key];
        clearField(key, placeholder);
        field.select.disabled = false;
        field.hint.textContent = message;
        DOWNSTREAM[key].forEach((child) => clearField(child, PLACEHOLDERS[child]));
    }

    function existingCaseWire() {
        return isExisting() ? "yes" : "no";
    }

    const LISTS = {
        case_category: {
            url: "/api/dropdowns/case-categories/",
            loading: "Loading case categories…",
            placeholder: "Choose a case category",
            params: () => ({
                court: fields.court.select.value,
                guessed_case_category: guesses["case category"] || "",
                existing_case: existingCaseWire(),
            }),
        },
        case_type: {
            url: "/api/dropdowns/case-types/",
            loading: "Loading case types…",
            placeholder: "Choose a case type",
            params: () => ({
                court: fields.court.select.value,
                parent: fields.case_category.select.value,
                guessed_case_type: guesses["case type"] || "",
                existing_case: existingCaseWire(),
            }),
        },
        filing_type: {
            url: "/api/dropdowns/filing-types/",
            loading: "Loading filing types…",
            placeholder: "Choose a filing type",
            params: () => ({
                court: fields.court.select.value,
                case_category: fields.case_category.select.value,
                case_type: fields.case_type.select.value,
                existing_case: existingCaseWire(),
                guessed_filing_type: guesses["filing type"] || "",
            }),
        },
    };

    async function loadList(key) {
        const field = fields[key];
        const list = LISTS[key];
        const token = supersede(key);
        const ancestors = ORDER.slice(0, ORDER.indexOf(key));
        if (ancestors.some((name) => !fields[name].select.value)) {
            [key, ...DOWNSTREAM[key]].forEach((name) => clearField(name, PLACEHOLDERS[name]));
            await loadFilerRoles();
            return;
        }
        // Everything below waits on this list, and says so in place rather
        // than folding open or closed while it does.
        startLoading(key, list.loading);
        DOWNSTREAM[key].forEach((child) => startLoading(child, gettext("Waiting for the choice above…")));
        if (field.mode === "edit") announce(list.loading);
        const controller = new AbortController();
        inFlight[key] = controller;
        let options;
        try {
            options = await getJson(`${list.url}?${new URLSearchParams({
                jurisdiction: context.jurisdiction,
                ...list.params(),
            })}`, controller.signal);
        } catch (error) {
            if (!isCurrent(key, token)) return;
            failField(key, error.message, list.placeholder);
            await loadFilerRoles();
            return;
        }
        if (!isCurrent(key, token)) return;
        delete inFlight[key];
        if (key === "case_category" && !options.length) {
            // Some courts in the court list take no filings at all. Saying
            // so beats leaving the filer with three dropdowns that will not
            // open and no idea which choice caused it.
            failField(key, gettext("This court does not accept filings through e-filing. Choose a different court above."), list.placeholder);
            fields[key].select.disabled = true;
            await loadFilerRoles();
            return;
        }
        await populate(key, options, list.placeholder, token);
    }

    async function populate(key, options, placeholder, token) {
        const field = fields[key];
        const firstLoad = !field.loaded;
        field.loaded = true;
        field.select.innerHTML = `<option value="">${escapeHtml(placeholder)}</option>`;
        options.forEach((item) => {
            const opt = new Option(optionText(item), optionValue(item));
            if (item.recommended || item.selected || item.default) opt.dataset.recommended = "true";
            field.select.add(opt);
        });
        field.select.disabled = options.length === 0;
        finishLoading(key);

        const all = Array.from(field.select.options);
        const wanted = field.current.code;
        const kept = wanted ? all.find((o) => o.value === wanted) : null;
        const recommended = all.find((o) => o.dataset.recommended);

        if (kept || (!wanted && recommended)) {
            field.select.value = (kept || recommended).value;
            remember(key);
            field.hint.textContent = field.defaultHint;
            // Only the page's first look folds a match into its summary. After
            // that a field stays the way the filer left it.
            if (firstLoad) setMode(key, "found");
        } else if (wanted && !firstLoad) {
            // The filer's choice is not on the new list. Said beside the field,
            // naming what went, rather than left to look like it never existed.
            const parent = fields[PARENT[key]];
            const message = interpolate(gettext("%(choice)s is not offered for the %(parent)s you chose, so it was cleared. Choose again."), {
                choice: field.current.text || wanted,
                parent: parent ? parent.label.toLowerCase() : "",
            }, true);
            field.current = {
                code: "",
                text: ""
            };
            field.nameInput.value = "";
            field.select.value = "";
            field.hint.textContent = message;
            setMode(key, "edit");
            announce(message);
        } else {
            const extractionHint = guesses[field.guessKey] ?
                "We found a hint in your document, but could not match it to a choice here." :
                "Our system did not pull a match for this field. Choose an option to continue.";
            field.hint.textContent = [extractionHint, field.defaultHint].filter(Boolean).join(" ");
            field.nameInput.value = "";
            setMode(key, "edit");
        }
        if (token !== undefined && !isCurrent(key, token)) return;
        availability.check();
        await ADVANCE[key]();
    }

    // --- Editing one choice -------------------------------------------------
    //
    // Edit opens a field's dropdown, and a choice there applies at once: what
    // depends on it reloads, keeping whatever still fits. The field stays
    // open, so the filer can see what they chose beside the fields it changed.

    const roleField = document.getElementById("filer-role-field");
    const roleOptions = document.getElementById("filer-role-options");
    // The side saved on the draft. The server asks for it again whenever the
    // chosen case type still offers it, so the screen must show it then too.
    const draftRole = context.filer_role || "";
    let savedRole = draftRole;
    let roleGeneration = 0;

    function chosenRole() {
        return roleOptions.querySelector('input[name="filer_role"]:checked')?.value || "";
    }

    function beginEdit(key) {
        const field = fields[key];
        setMode(key, "edit");
        // The Edit button lives inside the display panel that setMode just
        // hid, so without this the click would strand focus on <body>.
        field.select.focus();
    }

    function pendingEdit() {
        // A court changed in the guided questions but not yet applied with
        // Update court. The other fields apply as they are chosen.
        return courtSelector && courtSelector.isEditing() ? fields.court : null;
    }

    // --- Loading -----------------------------------------------------------

    let courtSelector = null;

    async function mountCourtSelector(field) {
        const container = document.getElementById("court-selector");
        if (!container || !window.courtSelector) return false;
        const selector = window.courtSelector.mount({
            container,
            jurisdiction: context.jurisdiction,
            select: field.select,
            nameInput: field.nameInput,
            onStatus: announce,
            labels: {
                apply: gettext("Update court"),
                cancel: gettext("Cancel"),
                applyNote: gettext("Updates this page only. Nothing is saved until you confirm and continue."),
                chooseFirst: gettext("Choose a court to update, or Cancel to keep the one you had."),
                wait: gettext("Still finding courts for that answer. Try Update again in a moment."),
                updated: gettext("Court updated:"),
                cancelled: gettext("Change cancelled. The court is back to"),
            },
        });
        const started = await selector.start(field.current.code || "", guesses.court || "");
        if (!started) {
            container.remove();
            return false;
        }
        courtSelector = selector;
        // The selector shows the court it settled on, and folds and reopens
        // its own questions with their own Update and Cancel, so the field's
        // found/edit panels have nothing left to say.
        field.guided = true;
        field.select.hidden = true;
        field.select.disabled = false; // a disabled select is never submitted
        field.display.remove();
        field.display = null;
        setMode("court", "edit");
        return true;
    }

    async function loadCourts() {
        const field = fields.court;
        // Where the jurisdiction configures guided questions, they replace the
        // flat list: the filer answers their own state's routing questions and
        // the selector writes the court into the same <select> the form posts.
        if (await mountCourtSelector(field)) return;
        const token = supersede("court");
        startLoading("court", "Loading courts…");
        try {
            const options = await getJson(`/api/dropdowns/courts/?${new URLSearchParams({
                jurisdiction: context.jurisdiction,
                guessed_court: guesses.court || "",
            })}`);
            if (!isCurrent("court", token)) return;
            await populate("court", options, PLACEHOLDERS.court, token);
        } catch (error) {
            if (!isCurrent("court", token)) return;
            failField("court", error.message, PLACEHOLDERS.court);
        }
    }

    // Which side of the case the filer is on. Only some case types have sides
    // -- an eviction is two different filings depending on who is making it --
    // and which ones depends on the case type chosen above, so the question
    // appears and disappears with it.
    function roleOptionHtml(role, chosen) {
        const hint = role.suggested && !chosen ?
            ` <em class="filer-role__hint">${gettext("probably you, from the document you uploaded")}</em>` :
            "";
        const description = role.description ? `<small>${escapeHtml(role.description)}</small>` : "";
        return `
      <label>
        <input type="radio" name="filer_role" value="${escapeHtml(role.id)}"${role.id === chosen ? " checked" : ""} />
        <span><strong>${escapeHtml(role.label)}${hint}</strong>${description}</span>
      </label>`;
    }

    async function loadFilerRoles() {
        const token = ++roleGeneration;
        // Keep an answer the filer already gave while they edit other fields.
        savedRole = chosenRole() || savedRole;
        const caseTypeName = fields.case_type.nameInput.value;
        if (!caseTypeName) {
            roleField.hidden = true;
            roleOptions.innerHTML = "";
            return;
        }
        let roles = [];
        try {
            roles = await getJson(`/api/filer-roles/?${new URLSearchParams({
                jurisdiction: context.jurisdiction,
                court: fields.court.select.value,
                case_category_name: fields.case_category.nameInput.value,
                case_type_name: caseTypeName,
                filing_type_name: fields.filing_type.nameInput.value,
            })}`);
        } catch (error) {
            // A case type with no sides is the norm, and so is the answer to
            // this call being nothing. Failing quietly leaves the filer with
            // the screen they had before, rather than an error about a
            // question most cases never ask.
            console.warn("Could not load the sides of this case:", error);
        }
        if (token !== roleGeneration) return;
        // Same rule as the server: ask when a side is suggested or the saved
        // side is on offer. An answer given here is kept while it is offered.
        const chosen = roles.some((role) => role.id === savedRole) ? savedRole : "";
        const needsConfirmation = Boolean(chosen) || roles.some((role) => role.suggested || role.id === draftRole);
        roleOptions.innerHTML = needsConfirmation ? roles.map((role) => roleOptionHtml(role, chosen)).join("") : "";
        roleField.hidden = !needsConfirmation;
    }

    const ADVANCE = {
        court: () => loadList("case_category"),
        case_category: () => loadList("case_type"),
        case_type: async () => {
            await loadList("filing_type");
        },
        filing_type: loadFilerRoles,
    };

    let applyingSearch = false;

    async function applySearchPath(path) {
        applyingSearch = true;
        try {
            if (courtSelector && !await courtSelector.start(path.court.code, "", {
                    requireMatch: true
                })) {
                throw new Error(gettext("This court could not be selected. Try again or use the court questions."));
            }
            const stage = form.querySelector(`input[name="existing_case"][value="${path.initial ? "new" : "existing"}"]`);
            if (stage && !stage.checked) {
                stage.checked = true;
                stage.dispatchEvent(new Event("change", {
                    bubbles: true
                }));
                // Make the changed answer visible beside the saved summary.
                if (pathQuestion) pathQuestion.hidden = false;
                changePath?.setAttribute("aria-expanded", "true");
            }
            // All four lists were validated together on the server. Publish
            // them together so old cascading requests cannot undo this choice.
            supersede("court");
            ORDER.forEach(key => {
                const field = fields[key];
                field.select.replaceChildren(new Option(gettext("Choose an option"), ""));
                path.options[key].forEach(item => field.select.add(new Option(item.name, item.code)));
                field.select.value = path[key].code;
                field.select.disabled = false;
                field.loaded = true;
                finishLoading(key);
                remember(key);
                field.hint.textContent = field.defaultHint;
                setMode(key, field.guided ? "edit" : "found");
            });
            await loadFilerRoles();
            availability.check();
            announce(gettext("Filing path updated. Check the choices, then confirm and continue to save them."));
        } finally {
            applyingSearch = false;
        }
    }

    ORDER.forEach((key) => {
        fields[key].select.addEventListener("change", () => {
            if (applyingSearch) return;
            remember(key);
            availability.check(fields[key].root);
            fields[key].hint.textContent = fields[key].defaultHint;
            ADVANCE[key]();
        });
    });

    form.querySelectorAll('input[name="existing_case"]').forEach((radio) => {
        radio.addEventListener("change", () => {
            if (applyingSearch) return;
            if (!isExisting() && fields.court.select.value) loadList("case_category");
        });
    });

    // The people the document named. Rows are added and removed here rather
    // than on a round trip so the filer can fix the whole caption at once;
    // the view reads the surviving rows back off the form.
    const partyList = document.getElementById("review-parties-list");
    const partyTemplate = document.getElementById("review-party-template");
    const partyEmpty = document.getElementById("review-parties-empty");
    const addPartyButton = document.getElementById("add-review-party");

    if (partyList && partyTemplate && partyEmpty && addPartyButton) {
        function syncPartyEmptyState() {
            partyEmpty.hidden = partyList.children.length > 0;
        }

        // "This is me" on one of the people the document named. At most one of
        // them can be, so choosing a row un-chooses whichever held it before.
        // The answer rides on a hidden value rather than a checkbox, so every row
        // posts one and the lists the view reads back stay index-aligned.
        function setIsMe(row, isMe) {
            const field = row.querySelector('input[name="party_is_self"]');
            const toggle = row.querySelector(".review-party__is-me-toggle");
            if (!field || !toggle) return;
            field.value = isMe ? "true" : "false";
            toggle.setAttribute("aria-pressed", isMe ? "true" : "false");
            toggle.classList.toggle("btn-outline-secondary", !isMe);
            toggle.classList.toggle("btn-primary", isMe);
        }

        function syncIsMeButtons() {
            Array.from(partyList.querySelectorAll(".review-party")).forEach((row) => {
                const field = row.querySelector('input[name="party_is_self"]');
                setIsMe(row, Boolean(field) && field.value === "true");
            });
        }

        partyList.addEventListener("click", (event) => {
            const isMeToggle = event.target.closest(".review-party__is-me-toggle");
            if (isMeToggle) {
                const row = isMeToggle.closest(".review-party");
                const field = row.querySelector('input[name="party_is_self"]');
                const turningOn = !field || field.value !== "true";
                Array.from(partyList.querySelectorAll(".review-party")).forEach((other) => {
                    setIsMe(other, turningOn && other === row);
                });
                return;
            }
            const removeButton = event.target.closest(".review-party__remove");
            if (!removeButton) return;
            const row = removeButton.closest(".review-party");
            // The button being clicked is inside the row about to disappear, so
            // send focus somewhere that will still exist afterwards.
            addPartyButton.focus();
            row.remove();
            syncPartyEmptyState();
        });

        syncIsMeButtons();

        addPartyButton.addEventListener("click", () => {
            partyList.appendChild(partyTemplate.content.cloneNode(true));
            syncPartyEmptyState();
            syncIsMeButtons();
            partyList.lastElementChild.querySelector('input[name="party_name"]').focus();
        });
    }

    // An existing case's court, classification and number come from the
    // court's records on lookup, so only a new case is described here.
    function updateCasePathSections() {
        const existing = isExisting();
        const proposal = document.getElementById("filing-proposal");
        if (proposal) {
            proposal.hidden = !existing;
            proposal.disabled = !existing;
        }
        const details = document.getElementById("new-case-details");
        const isNew = chosenPath() === "new";
        details.hidden = !isNew;
        details.disabled = !isNew;
        const parties = document.getElementById("review-parties");
        if (parties) {
            parties.hidden = existing;
            parties.disabled = existing;
        }
        document.getElementById("existing-case-next").hidden = !existing;
        availability.check();
    }

    const PATH_ERROR = gettext("Choose whether this is a new or existing court case to continue.");

    form.querySelectorAll('input[name="existing_case"]').forEach((radio) => {
        radio.addEventListener("change", () => {
            // Answering the question clears the error that asked for it.
            if (errorBox.textContent === PATH_ERROR) errorBox.hidden = true;
            updateCasePathSections();
        });
    });
    updateCasePathSections();

    // A choice changed on the page but never applied, or a list still on its
    // way, would reach the court as something the filer did not see.
    function blockUnfinished(event) {
        const unapplied = pendingEdit();
        const loading = ORDER.some((key) => fields[key].loading);
        if (!unapplied && !loading) return false;
        event.preventDefault();
        let target = errorBox;
        if (unapplied) {
            errorBox.textContent = interpolate(gettext("You changed the %(label)s but did not apply it. Choose Update to use it, or Cancel to keep what you had."), {
                label: unapplied.label.toLowerCase()
            }, true);
            target = document.querySelector("[data-court-apply]") || errorBox;
        } else {
            errorBox.textContent = gettext("The court's lists are still loading. Wait a moment, then continue.");
        }
        errorBox.hidden = false;
        target.scrollIntoView({
            behavior: "smooth",
            block: "center"
        });
        if (target !== errorBox) target.focus({
            preventScroll: true
        });
        return true;
    }

    form.addEventListener("submit", (event) => {
        if (isExisting()) return;
        if (!chosenPath()) {
            event.preventDefault();
            errorBox.textContent = PATH_ERROR;
            errorBox.hidden = false;
            openPathQuestion("");
            return;
        }
        if (blockUnfinished(event)) return;
        const missingCase = !fields.court.select.value || !fields.case_category.select.value || !fields.case_type.select.value;
        const missingRole = !roleField.hidden && !chosenRole();
        if (!missingCase && !missingRole) return;

        event.preventDefault();
        errorBox.textContent = missingCase ?
            "Choose a court, case category, and case type from the lists to continue." :
            "Choose which side of this case you are on to continue.";
        errorBox.hidden = false;
        (missingCase ? errorBox : roleField).scrollIntoView({
            behavior: "smooth",
            block: "center"
        });
    });

    // The acknowledgement sits above the form, so a filer pressing Continue at
    // the bottom never sees the browser's own tooltip. Native validation fires
    // "invalid" before "submit", so this is where the error has to be shown.
    function showAcknowledgementError() {
        acknowledgement.setAttribute("aria-invalid", "true");
        acknowledgement.setAttribute("aria-describedby", acknowledgementError.id);
        acknowledgementError.hidden = false;
        acknowledgementError.parentElement.classList.add("extraction-acknowledgment--invalid");
        acknowledgement.focus({
            preventScroll: true
        });
        acknowledgementError.parentElement.scrollIntoView({
            behavior: "smooth",
            block: "center"
        });
    }

    function clearAcknowledgementError() {
        acknowledgement.removeAttribute("aria-invalid");
        acknowledgement.removeAttribute("aria-describedby");
        acknowledgementError.hidden = true;
        acknowledgementError.parentElement.classList.remove("extraction-acknowledgment--invalid");
    }

    if (acknowledgement && acknowledgementError) {
        acknowledgement.addEventListener("invalid", (event) => {
            event.preventDefault();
            showAcknowledgementError();
        });
        acknowledgement.addEventListener("change", () => {
            if (acknowledgement.checked) clearAcknowledgementError();
        });
        // The server sent the page back for this: take the filer straight to it.
        if (!acknowledgementError.hidden) showAcknowledgementError();
    }

    // The new-or-existing answer the filer already gave is shown as a
    // summary. Change opens the question; the document-conflict note's button
    // opens it with the other answer chosen, for the filer to confirm.
    const pathQuestion = document.getElementById("path-question");
    const changePath = document.getElementById("change-filing-path");
    const applySuggestion = document.getElementById("apply-path-suggestion");

    function openPathQuestion(value) {
        if (!pathQuestion) return;
        pathQuestion.hidden = false;
        if (changePath) changePath.setAttribute("aria-expanded", "true");
        const radio = value ?
            pathQuestion.querySelector(`input[name="existing_case"][value="${CSS.escape(value)}"]`) :
            pathQuestion.querySelector('input[name="existing_case"]:checked');
        if (value && radio && !radio.checked) {
            radio.checked = true;
            radio.dispatchEvent(new Event("change", {
                bubbles: true
            }));
        }
        (radio || pathQuestion.querySelector('input[name="existing_case"]'))?.focus();
    }

    if (changePath) changePath.addEventListener("click", () => openPathQuestion(""));
    if (applySuggestion) applySuggestion.addEventListener("click", () => openPathQuestion(applySuggestion.dataset.value));

    // The guard blocks until a check runs. A guided selector with no court
    // chosen yet fires no change, so check once the courts are in place.
    let newCaseStarted = false;

    function startNewCase() {
        if (isExisting()) {
            availability.check();
            return;
        }
        if (newCaseStarted) return;
        newCaseStarted = true;
        loadCourts().finally(() => {
            availability.check();
            window.filingCodeSearch?.mount({
                jurisdiction: context.jurisdiction,
                existingCase: existingCaseWire,
                applyPath: applySearchPath,
                zipShortcuts: context.zip_shortcuts || [],
            });
        });
    }
    form.querySelectorAll('input[name="existing_case"]').forEach(radio => radio.addEventListener("change", startNewCase));
    startNewCase();
})();