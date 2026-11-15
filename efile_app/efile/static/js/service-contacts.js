/* Keep the optional recipient editor off the service-method question. */
(() => {
    if (!window.HTMLDialogElement || !HTMLDialogElement.prototype.showModal) return;
    let dialog;
    let busy = false;
    let changed = false;
    let opener;
    const text = message => typeof gettext === "function" ? gettext(message) : message;
    const summary = () => document.getElementById("email-recipients");

    function closeEditor() {
        if (busy) return;
        dialog.close();
        dialog.remove();
        dialog = null;
        changed = false;
        (summary()?.querySelector('[data-service-open]') || opener)?.focus();
    }

    function updateChoices() {
        const form = dialog.querySelector('#service-recipients-form');
        if (!form) return;
        if (!form.elements.service_rules_checked || !form.elements.service_type) return;
        form.elements.service_rules_checked.required = !!form.querySelector('[name="service_contact"]:checked');
        const code = form.elements.service_type.value;
        form.querySelectorAll('[data-service-type]').forEach(note => {
            note.hidden = note.dataset.serviceType !== code;
        });
    }

    function showEditor(editor) {
        dialog = document.createElement('dialog');
        dialog.className = 'service-dialog';
        dialog.setAttribute('aria-labelledby', 'service-editor-title');
        dialog.append(editor);
        document.body.append(dialog);
        dialog.addEventListener('cancel', event => {
            event.preventDefault();
            closeEditor();
        });
        dialog.addEventListener('click', event => {
            if (!event.target.closest('[data-service-cancel]')) return;
            event.preventDefault();
            closeEditor();
        });
        dialog.addEventListener('change', () => {
            changed = true;
            updateChoices();
        });
        dialog.addEventListener('input', () => {
            changed = true;
        });
        dialog.addEventListener('submit', saveForm);
        updateChoices();
        dialog.showModal();
        editor.querySelector('h2').focus();
    }

    function showError(message) {
        const error = dialog?.querySelector('.service-editor-error') || summary()?.querySelector('.service-status');
        if (!error) return;
        error.textContent = message;
        error.hidden = false;
        error.tabIndex = -1;
        error.focus();
    }

    function appendContact(contact) {
        const choices = dialog.querySelector('#service-contact-choices');
        let input = Array.from(choices.querySelectorAll('input')).find(item => item.value === contact.contact_id);
        if (!input) {
            const label = document.createElement('label');
            label.className = 'service-contact-row';
            input = document.createElement('input');
            input.type = 'checkbox';
            input.name = 'service_contact';
            input.value = contact.contact_id;
            const detail = document.createElement('span');
            const name = document.createElement('strong');
            name.textContent = contact.name;
            const email = document.createElement('small');
            email.textContent = contact.email;
            detail.append(name, email);
            label.append(input, detail);
            choices.querySelector('[data-service-empty]')?.remove();
            choices.append(label);
        }
        return input;
    }

    async function saveForm(event) {
        event.preventDefault();
        if (busy) return;
        const form = event.target;
        if (form.id === 'service-recipients-form' && Array.from(dialog.querySelectorAll('input[form="service-add-form"]')).some(input => input.value.trim())) {
            showError(text('Add the new contact first, or clear their details before saving.'));
            return;
        }
        let focusAfterSave;
        const data = new FormData(form);
        const adding = form.id === 'service-add-form';
        let action = form.id === 'service-clear-form' ? 'clear_contacts' : 'save_contacts';
        if (adding) action = 'create_contact';
        data.set('action', action);
        busy = true;
        dialog.setAttribute('aria-busy', 'true');
        const buttons = Array.from(dialog.querySelectorAll('button, input, select'));
        const previouslyDisabled = new Set(buttons.filter(button => button.disabled));
        buttons.forEach(button => {
            button.disabled = true;
        });
        dialog.querySelector('.service-editor-error').hidden = true;
        try {
            const response = await fetch(form.getAttribute('action'), {
                method: 'POST',
                body: data,
                headers: {
                    'X-Requested-With': 'XMLHttpRequest'
                },
            });
            if (response.redirected) throw new Error(text('Your session changed. Reload the Service page and try again.'));
            if (!response.headers.get('content-type')?.includes('application/json')) throw new Error(text('We could not save your changes. Reload the Service page and try again.'));
            const result = await response.json();
            if (!response.ok || !result.success) throw new Error(result.error || text('We could not save your changes. Try again.'));
            if (adding) {
                const input = appendContact(result.contact);
                form.reset();
                dialog.querySelector('.service-add-contact').open = false;
                focusAfterSave = input;
                dialog.querySelector('[data-service-add-status]').textContent = text('Contact added. Select them to send copies.');
            } else {
                const parsed = new DOMParser().parseFromString(result.summary_html, 'text/html');
                const replacement = parsed.getElementById('email-recipients');
                if (!replacement?.querySelector('.service-status') || !summary()) throw new Error(text('Recipients were saved. Reload this page to see the list.'));
                summary().replaceWith(replacement);
                document.dispatchEvent(new CustomEvent("service-recipients-saved", {
                    detail: {
                        selected: !!replacement.querySelector(".service-summary__list")
                    }
                }));
                busy = false;
                closeEditor();
                summary().querySelector('.service-status').textContent = text('Recipients saved. No documents have been sent yet.');
            }
        } catch (error) {
            showError(error.message || text('We could not save your changes. Try again.'));
        } finally {
            busy = false;
            dialog?.removeAttribute('aria-busy');
            buttons.forEach(button => {
                button.disabled = previouslyDisabled.has(button);
            });
            focusAfterSave?.focus();
        }
    }

    document.addEventListener('click', async event => {
        const link = event.target.closest('[data-service-open]');
        if (!link) return;
        event.preventDefault();
        if (busy || dialog) return;
        opener = link;
        busy = true;
        link.setAttribute('aria-busy', 'true');
        const status = summary()?.querySelector('.service-status');
        if (!status) {
            busy = false;
            link.removeAttribute('aria-busy');
            return;
        }
        status.textContent = text('Loading contacts…');
        try {
            const response = await fetch(link.href);
            if (response.redirected) throw new Error(text('Your session changed. Reload the Service page and try again.'));
            const parsed = new DOMParser().parseFromString(await response.text(), 'text/html');
            const editor = parsed.getElementById('service-editor');
            if (!response.ok || !editor) throw new Error(text('We could not load contacts. Try again or reload this page.'));
            status.textContent = '';
            showEditor(editor);
        } catch (error) {
            status.textContent = error.message;
        } finally {
            busy = false;
            link.removeAttribute('aria-busy');
        }
    });

    // Review links and the no-JavaScript fallback use the same server-rendered editor.
    const initial = document.getElementById('service-editor');
    if (initial) showEditor(initial);
    window.addEventListener('beforeunload', event => {
        if (dialog && changed) {
            event.preventDefault();
            event.returnValue = '';
        }
    });
})();