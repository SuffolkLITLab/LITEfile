(function() {
    const STEPS = {
        filing: {
            number: 1,
            title: () => gettext("What are you filing?")
        },
        path: {
            number: 2,
            title: () => gettext("Choose your court and case type")
        },
        confirm: {
            number: 3,
            title: () => gettext("Check your choices")
        },
    };
    // Up to this many courts show as cards; more fall back to a dropdown.
    const COURT_CARD_LIMIT = 6;
    // "What do you want to do?" is asked only once results are this many.
    const ACTION_QUESTION_MIN = 8;
    // A question with more choices than this shows the most used ones first.
    const CHIPS_SHOWN = 5;
    // Choices a court may code as separate case types, asked in this order and
    // only when the court's paths differ on them. See path_qualifiers().
    const PATH_QUESTIONS = [{
        key: "amount",
        label: () => gettext("How much are you asking for?"),
        options: {}
    }, {
        key: "representation",
        label: () => gettext("Do you have a lawyer?"),
        options: {
            self: () => gettext("No, I'm representing myself"),
            lawyer: () => gettext("Yes")
        }
    }, {
        key: "jury",
        label: () => gettext("Do you want a jury?"),
        options: {
            none: () => gettext("No jury"),
            jury: () => gettext("Jury"),
            6: () => gettext("Jury of 6"),
            12: () => gettext("Jury of 12")
        }
    }, {
        key: "government",
        label: () => gettext("Filing for a government body?"),
        options: {
            no: () => gettext("No"),
            yes: () => gettext("Yes")
        },
        // Almost no one filing here is; the answer stays visible to change.
        assume: "no"
    }];
    const ZIP_PATTERN = /^\d{5}(?:-\d{4})?$/;

    function mount({
        jurisdiction,
        existingCase,
        applyPath,
        zipShortcuts = []
    }) {
        const dialog = document.getElementById("code-search-dialog");
        const open = document.getElementById("open-code-search");
        if (!dialog || !open) return;
        const byId = id => document.getElementById(id);
        const title = byId("code-search-title");
        const stepLabel = byId("code-search-step");
        const panels = {
            filing: byId("code-search-step-filing"),
            path: byId("code-search-step-path"),
            confirm: byId("code-search-step-confirm"),
        };
        const query = byId("code-search-query");
        const status = byId("code-search-status");
        const results = byId("code-search-results");
        const more = byId("code-search-more");
        const otherStage = byId("code-search-other-stage");
        const suggestions = byId("code-search-suggestions");
        const postalCode = byId("code-search-zip");
        const zipStatus = byId("code-search-zip-status");
        const zipShortcutList = byId("code-search-zip-shortcuts");
        const stageLabel = byId("code-search-stage-label");
        const stageToggle = byId("code-search-stage-toggle");
        const moreFilters = byId("code-search-more-filters");
        const filterPanel = byId("code-search-filters");
        const caseFilters = byId("code-search-case-filters");
        const summary = byId("code-search-summary");
        const courtsBox = byId("code-search-courts");
        const pathStatus = byId("code-search-path-status");
        const pathQuestions = byId("code-search-path-questions");
        const caseTypes = byId("code-search-case-types");
        const paths = byId("code-search-paths");
        const morePaths = byId("code-search-more-paths");
        const confirmList = byId("code-search-confirm");
        const explanation = byId("code-search-explanation");
        const applyStatus = byId("code-search-apply-status");
        const cancel = byId("cancel-code-search");
        const back = byId("code-search-back");
        const next = byId("code-search-next");
        const apply = byId("apply-code-search");

        let step = "filing";
        let stage = "no";
        let documentKind = "";
        let action = "";
        let category = "";
        const expanded = new Set();
        let showAll = false;
        let strongTotal = 0;
        let activeZip = "";
        let caseTopic = "";
        let caseChoices = {};
        let caseFilterSchema = "";
        let lastFacets = null;
        let lastCounties = [];
        let generation = 0;
        let controller;
        let timer;
        let offset = 0;
        let chosen = null;
        let selected = null;
        let pathGeneration = 0;
        let pathController;
        let loadedFor = null;
        let court = "";
        let pathOffset = 0;
        let courtPaths = [];
        let pathAnswers = {};
        let applying = false;

        function element(tag, text, className) {
            const node = document.createElement(tag);
            if (text) node.textContent = text;
            if (className) node.className = className;
            return node;
        }

        function link(text, href) {
            const node = element("a", text);
            node.href = href;
            node.target = "_blank";
            node.rel = "noopener noreferrer";
            return node;
        }

        function chip(text, pressed, data) {
            const button = element("button", text, "code-search__chip");
            button.type = "button";
            button.setAttribute("aria-pressed", String(pressed));
            Object.assign(button.dataset, data);
            return button;
        }

        function pressOnly(button) {
            for (const sibling of button.parentElement.querySelectorAll(".code-search__chip")) {
                sibling.setAttribute("aria-pressed", String(sibling === button));
            }
        }

        function list(items) {
            return items.length > 1 ?
                interpolate(gettext("%(first)s and %(last)s"), {
                    first: items.slice(0, -1).join(", "),
                    last: items.at(-1)
                }, true) : items[0] || "";
        }

        async function request(params, signal) {
            const response = await fetch(`/api/filing-code-search/?${new URLSearchParams({
                jurisdiction, zip: activeZip, existing_case: stage, document: documentKind, grouped: "true",
                case_topic: caseTopic, case_filters: JSON.stringify(caseChoices), action, category, strong: String(!showAll), ...params,
            })}`, {
                signal,
                headers: {
                    Accept: "application/json"
                }
            }).catch(error => {
                if (error.name === "AbortError") throw error;
                const unavailable = new Error(gettext("This helper could not be loaded. Try again shortly."));
                unavailable.retryable = true;
                throw unavailable;
            });
            if (!response.headers.get("content-type")?.includes("application/json")) {
                const error = new Error(gettext("This helper could not be loaded. Refresh the page and try again."));
                error.retryable = response.status >= 500;
                throw error;
            }
            const data = await response.json();
            if (!response.ok) {
                const error = new Error(data.error || gettext("This helper could not be loaded. Try again."));
                error.retryable = response.status === 503;
                throw error;
            }
            return data;
        }

        // Steps

        function showStep(name, {
            focus = true
        } = {}) {
            step = name;
            for (const [key, panel] of Object.entries(panels)) panel.hidden = key !== name;
            stepLabel.textContent = interpolate(gettext("Step %(number)s of 3"), {
                number: STEPS[name].number
            }, true);
            title.textContent = STEPS[name].title();
            cancel.hidden = name !== "filing";
            back.hidden = name === "filing";
            next.hidden = name === "confirm";
            apply.hidden = name !== "confirm";
            updateNext();
            if (focus) title.focus();
        }

        function updateNext() {
            next.textContent = step === "path" || chosen?.path ?
                gettext("Next: check and use") : gettext("Next: court and case type");
            next.disabled = step === "filing" ? !chosen : !selected;
            apply.disabled = !selected || applying;
        }

        function goForward() {
            if (step === "filing" && chosen?.path) {
                selected = chosen.path;
                renderConfirm();
                showStep("confirm");
            } else if (step === "filing" && chosen) {
                showStep("path");
                if (loadedFor !== chosen) loadCourts();
            } else if (step === "path" && selected) {
                renderConfirm();
                showStep("confirm");
            }
        }

        function goBack() {
            if (step === "confirm" && !chosen?.path) showStep("path");
            else showStep("filing");
        }

        // Step 1: search, ZIP, and case questions

        function invalidate() {
            generation += 1;
            controller?.abort();
            controller = null;
            clearTimeout(timer);
            chosen = null;
            resetPaths();
            more.hidden = true;
            updateNext();
        }

        function resetPaths() {
            pathGeneration += 1;
            pathController?.abort();
            pathController = null;
            loadedFor = null;
            selected = null;
        }

        function clearResults() {
            results.replaceChildren();
        }

        // "What does “subrogation” mean?", from the state's glossary.
        function addGlossary(terms, target) {
            if (!terms?.length) return;
            const details = element("details", "", "code-search__glossary");
            details.append(element("summary", terms.length === 1 ?
                interpolate(gettext("What does “%(term)s” mean?"), {
                    term: terms[0].label.toLowerCase()
                }, true) : gettext("What do these terms mean?")));
            const definitions = element("dl");
            for (const term of terms) {
                const meaning = element("dd", term.text);
                if (term.source) meaning.append(" ", link(gettext("Learn more"), term.source));
                definitions.append(element("dt", term.label), meaning);
            }
            details.append(definitions);
            target.append(details);
        }

        function addMeanings(data, target) {
            addGlossary(data.glossary, target);
            const meanings = (data.concepts || []).filter(item => item.text);
            const variants = data.variants || [];
            if (!meanings.length && variants.length < 2) return;
            const details = element("details", "", "code-search__concept-info");
            details.append(element("summary", gettext("About this filing type")));
            for (const concept of meanings) {
                const definition = element("div", "", "code-search__meaning");
                let scope = gettext("General meaning");
                if (concept.scope === "county") scope = interpolate(gettext("Meaning in %(name)s County"), {
                    name: concept.scope_name
                }, true);
                else if (concept.scope !== "general") scope = interpolate(gettext("Meaning in %(name)s"), {
                    name: concept.scope_name
                }, true);
                definition.append(element("p", `${concept.label} — ${scope}: ${concept.text}`));
                if (concept.source) definition.append(link(gettext("About this concept"), concept.source));
                details.append(definition);
            }
            if (variants.length > 1) details.append(element("p", gettext("Also listed as:") + " " + variants.slice(1).join("; ")));
            target.append(details);
        }

        // Only when the search found this through a different word, such as
        // "unlawful detainer" finding an eviction filing.
        function relatedTerms(data) {
            const reason = data.match_reason?.kind === "related_concept" ? data.match_reason : null;
            if (!reason) return "";
            const asked = words(reason.query);
            const labels = (data.concepts || [])
                .filter(item => reason.concepts.includes(item.key) && words(item.label) !== asked)
                .map(item => item.label.toLowerCase());
            return labels.length ? interpolate(gettext("Related to %(terms)s"), {
                terms: list(labels)
            }, true) : "";
        }

        function words(text) {
            return String(text || "").toLowerCase().replace(/[^a-z0-9]+/g, " ").trim();
        }

        function contextSummary(data) {
            const contexts = data.case_contexts || [];
            if (!contexts.length) return "";
            return data.case_context_count > 1 ?
                interpolate(gettext("%(count)s case types, including %(context)s"), {
                    context: contexts[0],
                    count: data.case_context_count
                }, true) : contexts[0];
        }

        function choiceRow(name, onChoose, disabled = false) {
            const row = element("div", "", "code-search__result");
            const label = element("label", "", "code-search__choice");
            const radio = element("input");
            radio.type = "radio";
            radio.name = "code-search-filing";
            radio.disabled = disabled;
            // A leading "Complaint / Petition - " that many results share is
            // muted (see markSharedPrefixes) so the words that differ stand out.
            const [, prefix, rest] = name.match(/^(.+?\s-\s)(.+)$/) || [null, "", name];
            const text = element("span", "", "code-search__name");
            if (prefix) {
                const muted = element("span", prefix, "code-search__name-prefix");
                muted.dataset.prefix = prefix;
                text.append(muted);
            }
            text.append(rest);
            label.append(radio, text);
            row.append(label);
            radio.addEventListener("change", onChoose);
            // The whole card chooses it, except the "About" disclosure and links.
            row.addEventListener("click", event => {
                if (!event.target.closest("details, a, label, input")) radio.click();
            });
            return {
                row,
                radio
            };
        }

        function addGroup(group, searchText) {
            const choice = {
                group,
                query: searchText
            };
            const {
                row
            } = choiceRow(group.name, () => {
                chosen = choice;
                updateNext();
            });
            row.classList.add("code-search__group");
            const meta = [contextSummary(group), relatedTerms(group)].filter(Boolean).join(" · ");
            if (meta) {
                const summaryLine = element("p", meta, "code-search__meta");
                summaryLine.title = meta;
                row.append(summaryLine);
            }
            addMeanings(group, row);
            results.append(row);
        }

        // Ungrouped results are already full paths, so they skip the court step.
        function addPath(path) {
            const {
                row
            } = choiceRow(path.filing_type.name, () => {
                chosen = {
                    path
                };
                updateNext();
            }, Boolean(path.unavailable));
            row.append(element("p", `${path.case_context || path.case_type.name} · ${path.court.name}`, "code-search__meta"));
            if (path.unavailable) row.append(element("p", path.unavailable));
            results.append(row);
        }

        function topicLabel(value) {
            return lastFacets?.case_topics.find(item => item.value === value)?.label || "";
        }

        // Only rows ranked by how many results they hold collapse; a fixed
        // question's choices always show.
        function question(key, label, options, pressed, collapsible = false) {
            const row = element("div", "", "code-search__question");
            row.setAttribute("role", "group");
            const name = element("span", label, "code-search__question-label");
            name.id = `code-search-question-${key}`;
            row.setAttribute("aria-labelledby", name.id);
            const chips = element("div", "", "code-search__chips");
            // "Not sure" (last) and the current answer always stay visible.
            const collapse = collapsible && options.length > CHIPS_SHOWN + 2 && !expanded.has(key);
            const shown = collapse ?
                options.filter((option, index) => index < CHIPS_SHOWN || index === options.length - 1 || option.value === pressed) :
                options;
            for (const option of shown) {
                const button = chip(option.label, option.value === pressed, {
                    caseFacet: key,
                    value: option.value
                });
                button.id = `code-search-chip-${key}-${option.value || "any"}`;
                if (option.count) button.append(element("span", String(option.count), "code-search__chip-count"));
                chips.append(button);
            }
            if (collapse) {
                const moreChips = element("button", interpolate(gettext("%(count)s more"), {
                    count: options.length - shown.length
                }, true), "code-search__chip code-search__chip--more");
                moreChips.type = "button";
                moreChips.id = `code-search-chip-${key}-more`;
                moreChips.dataset.expand = key;
                moreChips.setAttribute("aria-label", interpolate(gettext("Show %(count)s more choices for %(question)s"), {
                    count: options.length - shown.length,
                    question: label
                }, true));
                chips.insertBefore(moreChips, chips.lastElementChild);
            }
            row.append(name, chips);
            caseFilters.append(row);
            return row;
        }

        function addFacetHelp(facet, open) {
            const facetHelp = element("details", "", "code-search__amount-help");
            const toggle = element("summary", gettext("About claim amounts"));
            facetHelp.id = `code-search-help-${facet.key}`;
            toggle.id = `${facetHelp.id}-toggle`;
            facetHelp.open = open;
            facetHelp.append(toggle, element("p", facet.help));
            if (facet.source) facetHelp.append(link(gettext("Court guidance"), facet.source));
            caseFilters.append(facetHelp);
        }

        function renderCaseFilters(data) {
            lastFacets = data;
            caseTopic = data.case_topic || "";
            const actions = data.actions || [];
            const askAction = action || (data.total >= ACTION_QUESTION_MIN && actions.length > 1);
            const askTopic = data.case_topics.length > 1;
            const categories = data.categories || [];
            const askCategory = category || categories.length > 1;
            const schema = JSON.stringify([caseTopic, caseChoices, action, category, [...expanded], askAction && actions, askCategory && categories, askTopic, data.case_topics, data.case_facets]);
            caseFilters.hidden = !askAction && !askTopic && !askCategory && !data.case_facets.length;
            if (schema === caseFilterSchema) return;
            caseFilterSchema = schema;
            const focused = caseFilters.contains(document.activeElement) ? document.activeElement.id : "";
            const opened = new Set(Array.from(caseFilters.querySelectorAll("details[open]"), item => item.id));
            caseFilters.replaceChildren();
            const notSure = gettext("Not sure");
            if (askAction) question("action", gettext("What do you want to do?"), [...actions, {
                value: "",
                label: notSure
            }], action);
            if (askTopic) question("topic", gettext("Kind of case"), [...data.case_topics, {
                value: "all",
                label: notSure
            }], caseTopic || "all", true);
            if (askCategory) question("category", gettext("Case category"), [...categories, {
                value: "",
                label: notSure
            }], category, true);
            for (const facet of data.case_facets) {
                question(facet.key, facet.label, [...facet.options, {
                    value: "",
                    label: notSure
                }], caseChoices[facet.key] || "");
                if (facet.help) addFacetHelp(facet, opened.has(`code-search-help-${facet.key}`));
            }
            if (focused) byId(focused)?.focus();
        }

        function resetCaseFilters() {
            caseTopic = "";
            caseChoices = {};
            caseFilterSchema = "";
            action = "";
            category = "";
            expanded.clear();
            lastFacets = null;
            caseFilters.hidden = true;
        }

        function showZipStatus(counties) {
            lastCounties = counties;
            const typed = postalCode.value.trim();
            zipStatus.textContent = "";
            if (typed && !ZIP_PATTERN.test(typed)) zipStatus.textContent = gettext("Enter all 5 digits.");
            else if (counties.length) zipStatus.textContent = interpolate(ngettext("%(zip)s is in %(names)s County.", "%(zip)s is in %(names)s counties.", counties.length), {
                zip: typed.slice(0, 5),
                names: list(counties)
            }, true);
            renderZipShortcuts();
        }

        function renderZipShortcuts() {
            const typed = postalCode.value.trim().slice(0, 5);
            const options = zipShortcuts.filter(item => item.zip !== typed);
            zipShortcutList.hidden = !options.length;
            zipShortcutList.replaceChildren();
            const own = options.find(item => item.kind === "address");
            const recent = options.filter(item => item.kind === "recent");
            const shortcut = (text, zip, label) => {
                const button = element("button", text, "code-search__link");
                button.type = "button";
                button.dataset.zipShortcut = zip;
                if (label) button.setAttribute("aria-label", label);
                return button;
            };
            if (own) {
                zipShortcutList.append(shortcut(interpolate(gettext("Use your ZIP (%(zip)s)"), {
                    zip: own.zip
                }, true), own.zip));
            }
            if (recent.length) {
                if (own) zipShortcutList.append(" · ");
                zipShortcutList.append(gettext("Recent:") + " ");
                recent.forEach((item, index) => {
                    if (index) zipShortcutList.append(", ");
                    zipShortcutList.append(shortcut(item.zip, item.zip, interpolate(gettext("Use recent ZIP %(zip)s"), {
                        zip: item.zip
                    }, true)));
                });
            }
        }

        function zipChanged() {
            const typed = postalCode.value.trim();
            if (typed && !ZIP_PATTERN.test(typed)) {
                showZipStatus([]);
                return;
            }
            if (typed === activeZip) return showZipStatus(lastCounties);
            activeZip = typed;
            invalidate();
            clearResults();
            showZipStatus([]);
            timer = setTimeout(() => search(), 350);
        }

        function showOtherStage(count) {
            otherStage.hidden = !count;
            otherStage.textContent = stage === "yes" ?
                interpolate(ngettext("%(count)s result in “new case” filings", "%(count)s results in “new case” filings", count), {
                    count
                }, true) :
                interpolate(ngettext("%(count)s result in “existing case” filings", "%(count)s results in “existing case” filings", count), {
                    count
                }, true);
        }

        function markSharedPrefixes() {
            const prefixes = Array.from(results.querySelectorAll("[data-prefix]"));
            const counts = {};
            for (const node of prefixes) counts[node.dataset.prefix] = (counts[node.dataset.prefix] || 0) + 1;
            for (const node of prefixes) node.classList.toggle("is-shared", counts[node.dataset.prefix] >= 3);
        }

        function showSearchSummary(data, text) {
            markSharedPrefixes();
            if (data.case_topics || data.categories) renderCaseFilters({
                case_topics: [],
                case_facets: [],
                ...data
            });
            showZipStatus(data.location_counties || []);
            strongTotal = data.strong_total || 0;
            const narrowed = !showAll && strongTotal && strongTotal < data.total;
            const shownTotal = narrowed ? strongTotal : data.total;
            if (!data.total) status.textContent = gettext("No matching filing types. Try fewer words or another term.");
            else if (narrowed) status.textContent = interpolate(ngettext("%(count)s filing type matches “%(query)s”", "%(count)s filing types match “%(query)s”", strongTotal), {
                count: strongTotal,
                query: text
            }, true);
            else status.textContent = interpolate(ngettext("%(total)s filing type", "%(total)s filing types", data.total), {
                total: data.total
            }, true);
            if (data.corrected_terms.length) status.textContent += ". " + gettext("Similar spellings were included.");
            if (data.stale) status.textContent += ". " + gettext("These lists may be out of date.");
            // Only the first page carries it; later pages keep the link as is.
            if (data.other_stage_total !== undefined && data.other_stage_total !== null) showOtherStage(data.other_stage_total);
            const remaining = data.total - offset;
            more.hidden = offset >= 10000 || remaining <= 0;
            more.textContent = offset < shownTotal ? gettext("Show more") : interpolate(ngettext("Show %(count)s more filing type used in these case types", "Show %(count)s more filing types used in these case types", remaining), {
                count: remaining
            }, true);
        }

        async function search(append = false) {
            const text = query.value.trim();
            if (!append) {
                invalidate();
                offset = 0;
                showAll = false;
                clearResults();
            }
            suggestions.hidden = Boolean(text);
            results.hidden = !text;
            if (!append) otherStage.hidden = true;
            if (!text) {
                status.textContent = "";
                return;
            }
            const token = generation;
            controller ||= new AbortController();
            more.hidden = true;
            status.textContent = gettext("Searching filing types…");
            try {
                const data = await request({
                    q: text,
                    offset
                }, controller.signal);
                if (token !== generation || !dialog.open) return;
                if (data.groups) data.groups.forEach(group => addGroup(group, text));
                else data.results.forEach(path => addPath(path));
                offset += (data.groups || data.results).length;
                showSearchSummary(data, text);
            } catch (error) {
                if (token === generation && error.name !== "AbortError") {
                    status.textContent = error.message;
                    if (error.retryable) {
                        status.textContent += " " + gettext("We will try again shortly.");
                        timer = setTimeout(() => search(append), 10000);
                    }
                }
            }
        }

        function showStage() {
            stageLabel.textContent = stage === "yes" ? gettext("Existing case") : gettext("New case");
        }

        // Step 2: court, then case type

        function answers() {
            const chosenLabels = [];
            const chosenAction = lastFacets?.actions?.find(item => item.value === action);
            if (chosenAction) chosenLabels.push(chosenAction.label);
            if (category) chosenLabels.push(category);
            if (caseTopic && caseTopic !== "all") chosenLabels.push(topicLabel(caseTopic));
            for (const facet of lastFacets?.case_facets || []) {
                const option = facet.options.find(item => item.value === caseChoices[facet.key]);
                if (option) chosenLabels.push(option.label);
            }
            if (documentKind) chosenLabels.push(filterPanel.querySelector(`[data-document="${documentKind}"]`).textContent.trim());
            return chosenLabels.filter(Boolean);
        }

        function renderSummary() {
            summary.replaceChildren();
            const add = (term, value) => {
                if (!value) return;
                summary.append(element("dt", term), element("dd", value));
            };
            add(gettext("Filing"), chosen.group.name);
            add(gettext("Case"), stage === "yes" ? gettext("Existing case") : gettext("New case"));
            if (activeZip) {
                add(gettext("Case ZIP"), lastCounties.length ? interpolate(gettext("%(zip)s · %(names)s"), {
                    zip: activeZip,
                    names: list(lastCounties)
                }, true) : activeZip);
            }
            add(gettext("Your answers"), answers().join(" · "));
        }

        async function loadCourts() {
            resetPaths();
            const target = chosen;
            loadedFor = target;
            const token = pathGeneration;
            pathController = new AbortController();
            renderSummary();
            courtsBox.replaceChildren();
            paths.replaceChildren();
            caseTypes.hidden = true;
            morePaths.hidden = true;
            updateNext();
            pathStatus.textContent = gettext("Loading courts…");
            try {
                const data = await request({
                    q: target.query,
                    group: target.group.key
                }, pathController.signal);
                if (token !== pathGeneration || !dialog.open) return;
                const preferred = court || byId("court_code")?.value;
                const courts = [...data.courts].sort((a, b) => (b.code === preferred) - (a.code === preferred));
                if (courts.some(item => item.code === preferred)) court = preferred;
                else court = courts.length === 1 ? courts[0].code : "";
                if (!courts.length) {
                    pathStatus.textContent = gettext("No court accepts this filing with your answers. Go back and change them.");
                    return;
                }
                renderCourts(courts);
                await loadPaths();
            } catch (error) {
                if (token === pathGeneration && error.name !== "AbortError") pathStatus.textContent = error.message;
            }
        }

        function renderCourts(courts) {
            const question = gettext("Which court will hear the case?");
            if (courts.length <= COURT_CARD_LIMIT) {
                const fieldset = element("fieldset", "", "code-search__courts");
                fieldset.append(element("legend", question));
                const grid = element("div", "", "code-search__court-cards");
                for (const item of courts) {
                    const label = element("label", "", "code-search__card");
                    const radio = element("input");
                    radio.type = "radio";
                    radio.name = "code-search-court";
                    radio.value = item.code;
                    radio.checked = item.code === court;
                    radio.addEventListener("change", () => {
                        court = item.code;
                        loadPaths();
                    });
                    label.append(radio, element("span", item.name));
                    grid.append(label);
                }
                fieldset.append(grid);
                courtsBox.append(fieldset);
            } else {
                const wrapper = element("div", "", "code-search__courts");
                const label = element("label", question);
                const select = element("select", "", "form-select");
                select.id = "code-search-court";
                label.htmlFor = select.id;
                const blank = element("option", gettext("Choose a court"));
                blank.value = "";
                select.append(blank);
                for (const item of courts) {
                    const option = element("option", item.name);
                    option.value = item.code;
                    select.append(option);
                }
                select.value = court;
                select.addEventListener("change", () => {
                    court = select.value;
                    loadPaths();
                });
                wrapper.append(label, select);
                courtsBox.append(wrapper);
            }
        }

        function addCaseType(path) {
            const row = element("div", "", "code-search__result code-search__path-choice");
            const label = element("label", "", "code-search__choice");
            const radio = element("input");
            radio.type = "radio";
            radio.name = "code-search-result";
            radio.value = path.id;
            radio.disabled = Boolean(path.unavailable);
            label.append(radio, element("span", path.case_context || path.case_type.name));
            row.append(label);
            const described = [];
            if ((path.filing_label || path.filing_type.name).toLowerCase() !== chosen.group.name.toLowerCase()) {
                const variant = element("p", interpolate(gettext("This court calls it “%(name)s”."), {
                    name: path.filing_type.name
                }, true), "code-search__variant");
                variant.id = `code-search-variant-${path.id}`;
                described.push(variant.id);
                row.append(variant);
            }
            if (path.case_description) {
                const description = element("p", path.case_description, "code-search__case-description");
                description.id = `code-search-description-${path.id}`;
                described.push(description.id);
                row.append(description);
            }
            if (described.length) radio.setAttribute("aria-describedby", described.join(" "));
            if (path.unavailable) row.append(element("p", path.unavailable));
            radio.addEventListener("change", () => {
                selected = path;
                updateNext();
            });
            paths.append(row);
        }

        function qualifierValue(path, key) {
            const value = path.qualifiers?.[key];
            return key === "amount" ? value?.value || "" : value || "";
        }

        function qualifierChoices(questionSpec, pathList) {
            const values = new Map();
            for (const path of pathList) {
                const value = qualifierValue(path, questionSpec.key);
                if (!value) continue;
                const label = questionSpec.key === "amount" ? path.qualifiers.amount.label : questionSpec.options[value]?.();
                values.set(value, label || value);
            }
            return values;
        }

        // Each question's choices, from the paths the earlier answers leave.
        function askedQuestions() {
            let remaining = courtPaths;
            const asked = [];
            for (const questionSpec of PATH_QUESTIONS) {
                const values = qualifierChoices(questionSpec, remaining);
                if (values.size < 2) continue;
                let answer = pathAnswers[questionSpec.key] ?? questionSpec.assume ?? "";
                if (!values.has(answer)) answer = "";
                asked.push({
                    ...questionSpec,
                    answer,
                    choices: [...values].sort(([a], [b]) => String(a).localeCompare(String(b), undefined, {
                        numeric: true
                    }))
                });
                // A path that doesn't say stays in, as with step 1's questions.
                if (answer) remaining = remaining.filter(path => [answer, ""].includes(qualifierValue(path, questionSpec.key)));
            }
            return {
                asked,
                remaining
            };
        }

        function renderCaseTypes() {
            const {
                asked,
                remaining
            } = askedQuestions();
            const focused = pathQuestions.contains(document.activeElement) ? document.activeElement.id : "";
            pathQuestions.replaceChildren();
            for (const item of asked) {
                const row = element("div", "", "code-search__question");
                row.setAttribute("role", "group");
                const name = element("span", item.label(), "code-search__question-label");
                name.id = `code-search-path-question-${item.key}`;
                row.setAttribute("aria-labelledby", name.id);
                const chips = element("div", "", "code-search__chips");
                for (const [value, label] of [...item.choices, ["", gettext("Not sure")]]) {
                    const button = chip(label, value === item.answer, {
                        pathQuestion: item.key,
                        value
                    });
                    button.id = `code-search-path-chip-${item.key}-${value || "any"}`;
                    chips.append(button);
                }
                row.append(name, chips);
                pathQuestions.append(row);
            }
            pathQuestions.hidden = !asked.length;
            if (focused) byId(focused)?.focus();
            if (selected && !remaining.includes(selected)) selected = null;
            paths.replaceChildren();
            const terms = new Map(remaining.flatMap(path => path.glossary || []).map(term => [term.key, term]));
            addGlossary([...terms.values()], paths);
            remaining.forEach(addCaseType);
            for (const radio of paths.querySelectorAll("input")) radio.checked = String(selected?.id) === radio.value;
            caseTypes.hidden = !remaining.length;
            pathStatus.textContent = remaining.length ?
                interpolate(ngettext("%(count)s case type fits", "%(count)s case types fit", remaining.length), {
                    count: remaining.length
                }, true) : gettext("No matching case types in this court.");
            updateNext();
        }

        async function loadPaths(append = false) {
            const token = ++pathGeneration;
            pathController?.abort();
            pathController = new AbortController();
            if (!append) {
                pathOffset = 0;
                courtPaths = [];
                paths.replaceChildren();
                pathQuestions.hidden = true;
                selected = null;
                updateNext();
            }
            morePaths.hidden = true;
            if (!court) {
                caseTypes.hidden = true;
                pathStatus.textContent = gettext("Choose a court to see its case types.");
                return;
            }
            pathStatus.textContent = gettext("Loading case types…");
            try {
                const data = await request({
                    q: chosen.query,
                    group: chosen.group.key,
                    court,
                    offset: pathOffset,
                    limit: 500
                }, pathController.signal);
                if (token !== pathGeneration || !dialog.open) return;
                courtPaths = courtPaths.concat(data.results);
                pathOffset += data.results.length;
                renderCaseTypes();
                morePaths.hidden = pathOffset >= data.total || pathOffset >= 10000;
            } catch (error) {
                if (token === pathGeneration && error.name !== "AbortError") pathStatus.textContent = error.message;
            }
        }

        // Step 3: check and use

        function renderConfirm() {
            confirmList.replaceChildren();
            applyStatus.textContent = "";
            const row = (term, value, note, changeStep, changeLabel) => {
                const item = element("div", "", "code-search__confirm-row");
                const detail = element("dd");
                const text = element("span", "", "code-search__confirm-value");
                text.append(element("span", value));
                if (note) text.append(element("span", note, "code-search__meta"));
                detail.append(text);
                if (changeStep) {
                    const change = element("button", gettext("Change"), "code-search__link");
                    change.type = "button";
                    change.dataset.codeSearchStep = changeStep;
                    change.append(element("span", " " + changeLabel, "visually-hidden"));
                    detail.append(change);
                }
                item.append(element("dt", term), detail);
                confirmList.append(item);
            };
            const viaGroup = !chosen?.path;
            row(gettext("Court"), selected.court.name, "", viaGroup && "path", gettext("court"));
            row(gettext("Case category"), selected.case_category.name);
            row(gettext("Case type"), selected.case_type.name, selected.case_description, viaGroup && "path", gettext("case type"));
            const renamed = viaGroup && selected.filing_type.name.toLowerCase() !== chosen.group.name.toLowerCase();
            row(gettext("Filing type"), selected.filing_type.name, renamed ? interpolate(gettext("This court's name for “%(name)s”"), {
                name: chosen.group.name
            }, true) : "", "filing", gettext("filing type"));
            explanation.replaceChildren();
            addGlossary(selected.glossary, explanation);
            if (selected.explanation) explanation.append(element("p", selected.explanation));
            if (selected.case_guidance?.source) explanation.append(link(gettext("About this case type"), selected.case_guidance.source));
            if (selected.explanation_source) explanation.append(link(gettext("Court information"), selected.explanation_source));
            explanation.hidden = !explanation.children.length;
        }

        // Events

        open.disabled = false;
        open.addEventListener("click", () => {
            stage = existingCase();
            showStage();
            showStep("filing", {
                focus: false
            });
            showZipStatus(lastCounties);
            dialog.showModal();
            query.focus();
            search();
        });
        query.addEventListener("input", () => {
            resetCaseFilters();
            invalidate();
            clearResults();
            status.textContent = gettext("Searching filing types…");
            if (!query.value.trim()) search();
            else timer = setTimeout(() => search(), 250);
        });
        query.addEventListener("keydown", event => {
            if (event.key === "Enter") {
                event.preventDefault();
                search();
            }
        });
        postalCode.addEventListener("input", zipChanged);
        zipShortcutList.addEventListener("click", event => {
            const button = event.target.closest("[data-zip-shortcut]");
            if (!button) return;
            postalCode.value = button.dataset.zipShortcut;
            postalCode.focus();
            zipChanged();
        });

        function switchStage() {
            stage = stage === "yes" ? "no" : "yes";
            showStage();
            search();
        }
        stageToggle.addEventListener("click", switchStage);
        otherStage.addEventListener("click", () => {
            switchStage();
            query.focus();
        });

        function showFilterToggle() {
            moreFilters.setAttribute("aria-expanded", String(!filterPanel.hidden));
            if (!filterPanel.hidden) moreFilters.textContent = gettext("Fewer filters");
            else moreFilters.textContent = documentKind ? gettext("More filters (1)") : gettext("More filters");
        }
        moreFilters.addEventListener("click", () => {
            filterPanel.hidden = !filterPanel.hidden;
            showFilterToggle();
        });
        filterPanel.addEventListener("click", event => {
            const button = event.target.closest("[data-document]");
            if (!button) return;
            documentKind = button.dataset.document;
            pressOnly(button);
            showFilterToggle();
            search();
        });
        caseFilters.addEventListener("click", event => {
            const expand = event.target.closest("[data-expand]");
            if (expand) {
                expanded.add(expand.dataset.expand);
                caseFilterSchema = "";
                renderCaseFilters(lastFacets);
                // Focus the first choice that was hidden.
                const chips = byId(`code-search-question-${expand.dataset.expand}`).nextElementSibling.children;
                chips[Math.min(CHIPS_SHOWN, chips.length - 1)]?.focus();
                return;
            }
            const button = event.target.closest("[data-case-facet]");
            if (!button) return;
            const key = button.dataset.caseFacet;
            pressOnly(button);
            if (key === "action") action = button.dataset.value;
            else if (key === "category") category = button.dataset.value;
            else if (key === "topic") {
                caseTopic = button.dataset.value;
                caseChoices = {};
            } else if (button.dataset.value) caseChoices[key] = button.dataset.value;
            else delete caseChoices[key];
            search();
        });
        suggestions.addEventListener("click", event => {
            const button = event.target.closest("[data-code-query]");
            if (!button) return;
            query.value = button.dataset.codeQuery;
            query.focus();
            search();
        });
        more.addEventListener("click", () => {
            if (offset >= strongTotal) showAll = true;
            search(true);
        });
        morePaths.addEventListener("click", () => loadPaths(true));
        pathQuestions.addEventListener("click", event => {
            const button = event.target.closest("[data-path-question]");
            if (!button) return;
            pathAnswers[button.dataset.pathQuestion] = button.dataset.value;
            renderCaseTypes();
        });
        next.addEventListener("click", goForward);
        back.addEventListener("click", goBack);
        dialog.addEventListener("click", event => {
            const button = event.target.closest("[data-code-search-step]");
            if (button && !applying) showStep(button.dataset.codeSearchStep);
        });

        function close() {
            if (!applying) dialog.close();
        }
        ["close-code-search", "cancel-code-search"].forEach(id => byId(id).addEventListener("click", close));
        dialog.addEventListener("cancel", event => {
            if (applying) event.preventDefault();
        });
        dialog.addEventListener("keydown", event => {
            if (event.key !== "Tab") return;
            const controls = Array.from(dialog.querySelectorAll("button, input, select, a[href], summary, [tabindex]:not([tabindex='-1'])"))
                .filter(node => node.tabIndex >= 0 && !node.matches(":disabled") && node.getClientRects().length);
            const first = controls[0];
            const last = controls[controls.length - 1];
            if (event.shiftKey && document.activeElement === first) {
                event.preventDefault();
                last?.focus();
            } else if (!event.shiftKey && document.activeElement === last) {
                event.preventDefault();
                first?.focus();
            }
        });
        dialog.addEventListener("close", () => {
            invalidate();
            open.focus();
        });
        apply.addEventListener("click", async () => {
            if (!selected || applying) return;
            applying = true;
            updateNext();
            back.disabled = true;
            panels.confirm.inert = true;
            applyStatus.textContent = gettext("Checking this path with the court…");
            try {
                const data = await request({
                    path_id: selected.id
                });
                await applyPath(data.path);
                dialog.close();
            } catch (error) {
                applyStatus.textContent = error.message;
            } finally {
                applying = false;
                back.disabled = false;
                panels.confirm.inert = false;
                updateNext();
            }
        });
    }

    function mountFilingTypeOnly({
        container,
        select,
        choices
    }) {
        if (!container) return;
        const input = container.querySelector(".filing-search");
        const results = container.querySelector(".filing-search-results");
        const status = container.querySelector(".filing-search-status");

        function search() {
            const words = input.value.toLocaleLowerCase().trim().split(/\s+/).filter(Boolean);
            const matches = [];
            for (const option of choices) {
                if (words.every(word => option.text.toLocaleLowerCase().includes(word))) matches.push(option);
            }
            results.replaceChildren();
            status.textContent = matches.length ? interpolate(gettext("%(count)s matching filing types"), {
                count: matches.length
            }, true) : gettext("No matching filing types. Try another word or use the dropdown.");
            for (const option of matches) {
                const row = document.createElement("li");
                const button = document.createElement("button");
                button.type = "button";
                button.className = "btn btn-link";
                button.textContent = option.text;
                button.addEventListener("click", () => {
                    select.value = option.value;
                    select.dispatchEvent(new Event("change", {
                        bubbles: true
                    }));
                    container.open = false;
                    select.focus();
                });
                row.append(button);
                results.append(row);
            }
        }
        input.addEventListener("input", search);
        search();
    }
    window.filingCodeSearch = {
        mountFilingTypeOnly,
        mount
    };
})();