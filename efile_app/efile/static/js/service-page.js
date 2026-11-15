/* Reveal the electronic-service tools only after the filer chooses to check them. */
(() => {
    const form = document.getElementById('service-method-form');
    if (!form) return;
    const details = document.getElementById('electronic-service-details');
    const rules = document.getElementById('service-state-rules');
    let dirty = false;
    const update = () => {
        const electronic = form.querySelector('[value="electronic"]').checked;
        details.hidden = !electronic;
        if (electronic) rules.open = true;
    };
    update();
    form.addEventListener('change', () => {
        dirty = true;
        update();
    });
    form.addEventListener('submit', () => {
        dirty = false;
    });
    document.addEventListener('service-recipients-saved', event => {
        dirty = !event.detail.selected;
        form.querySelector("[data-service-removal-note]").hidden = !event.detail.selected;
    });
    document.addEventListener('click', event => {
        if (event.target.closest('[data-service-open]')) dirty = false;
    });
    window.addEventListener('beforeunload', event => {
        if (!dirty) return;
        event.preventDefault();
        event.returnValue = '';
    });
})();