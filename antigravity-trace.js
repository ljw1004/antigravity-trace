const TITLE = Symbol('TITLE');
const INLINE = Symbol('INLINE');
// Invariant: the contents of [TITLE] and [INLINE] have both been escaped

function fromHTML(html) {
    const t = document.createElement('template');
    t.innerHTML = html.trim();
    return t.content.firstElementChild;
}

function esc(s) {
    return s.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/\n/g, "<br/>").replace(/\\n/g, "<br/>");
}

function render(data, label) {
    if (data?.[TITLE] !== undefined) {
        return data;
    } else if (data?.label === "STDIO") {
        return {
            [TITLE]: `[${data?.time}] `,
            [INLINE]: esc(data?.endpoint + ' ' + String(data?.request ?? data?.response ?? '')),
        };
    } else if (Array.isArray(data?.functionDeclarations) && data.functionDeclarations.length === 1) {
        return {
            [TITLE]: esc(data.functionDeclarations[0].name),
            body: {
                '<description>': data.functionDeclarations[0].description,
                ...data.functionDeclarations[0].parameters.properties
            }
        };
    } else if (data?.label === "CLOUD" && data?.endpoint?.includes('streamGenerateContent')) {
        const body = [];
        const request = data?.request?.request ?? data?.request?.['*request'] ?? {};
        const sys = request?.['systemInstruction'] ?? request?.['*systemInstruction'];
        if (sys) {
            body.push({ [TITLE]: 'systemInstruction', body: (sys?.['parts'] ?? sys?.['*parts'] ?? []).map(p => p?.text ?? p).join('\n') });
        }
        const tools = request?.['tools'] ?? request?.['*tools'];
        if (tools && Array.isArray(tools)) {
            if (tools.length === 1 && tools[0] === '...') {
                body.push({ [TITLE]: 'tools', [INLINE]: ' [...unchanged]', body: ['...unchanged'] });
            } else {
                body.push({ [TITLE]: 'tools', [INLINE]: ` [...${tools.length} items]`, body: tools });
            }
        }
        function pushContent(prefix, contents) {
            if (!Array.isArray(contents) || contents.length === 0 || contents[0] === '...') return;
            for (const content of contents) {
                const parts = content?.parts.map(p => {
                    if (p?.text) {
                        return { rawTitle: '', rawInline: p?.text ?? '', body: p?.text };
                    } else if (p?.functionResponse) {
                        return { rawTitle: `${p?.functionResponse?.name}():result`, body: { [TITLE]: esc(`${p?.functionResponse?.name}():result. `), [INLINE]: esc(p?.functionResponse?.response?.output?.slice(0, 80) ?? ''), body: p?.functionResponse?.response?.output } };
                    } else if (p?.functionCall) {
                        return { rawTitle: `${p?.functionCall?.name}()`, body: { [TITLE]: esc(`${p?.functionCall?.name}(...)`), [INLINE]: '', body: p?.functionCall?.args } };
                    } else {
                        return { rawTitle: '??', body: p };
                    }
                });
                body.push({
                    [TITLE]: esc(`${prefix}${content.role}: ${parts.map(p => p.rawTitle).filter(p => p).join(', ')}`),
                    [INLINE]: esc(parts.map(p => p.rawInline).filter(p => p).join(", ").slice(0, 80)),
                    body: parts.map(p => p.body)
                });
            }
        }
        pushContent('', request?.['contents']);
        pushContent('-', request?.['contents-']);
        pushContent('+', request?.['contents+']);
        let responseText = "";
        let responseCalls = [];
        const responses = typeof data?.response === 'undefined' ? [] : Array.isArray(data?.response) ? data.response : [data.response];
        for (const response of responses) {
            for (const candidate of response?.response?.candidates ?? []) {
                for (const p of candidate?.content?.parts ?? []) {
                    if (p?.text) responseText += p?.text;
                    else if (p?.functionCall) responseCalls.push({ [TITLE]: esc(`${p?.functionCall?.name}()`), [INLINE]: '', body: p?.functionCall?.args });
                }
            }
        }
        body.push({
            [TITLE]: 'response: ',
            [INLINE]: [...responseCalls.map(c => c[TITLE]), esc(responseText.slice(0, 80))].join(', '),
            body: [...responseCalls, responseText],
        });
        return {
            [TITLE]: esc(`[${data?.time}] ${data?.endpoint} [${data?.request?.model ?? ''}, ${data?.duration ?? ''}s]`),
            [INLINE]: '',
            body: [...body, { [TITLE]: "[raw]", [INLINE]: "", body: data }],
            open: true,
        };
    } else if (data?.label !== undefined && data?.endpoint !== undefined && data?.time !== undefined) {
        return {
            [TITLE]: esc(`[${data?.time}] ${data?.label} ${data?.endpoint}`),
            [INLINE]: '',
            body: data,
        };
    } else {
        return {
            [TITLE]: esc(label),
            [INLINE]: esc(
                Array.isArray(data)
                    ? `[...${data.length} items]`
                    : '{' + Object.keys(data).map(k => `${JSON.stringify(k)}:`).join(',') + '}'
            ),
            body: data,
            numbered: true,
        }
    }
}

function buildNode(data, label) {
    if (data && typeof data === 'object') {
        const r = render(data, label);
        const d = fromHTML(`<details><summary>${r[TITLE]}<output>${r[INLINE] ?? ''}</output></summary></details>`);
        d.addEventListener('toggle', () => {
            if (r.body === undefined) {
                // skip
            } else if (Array.isArray(r.body)) {
                r.body.forEach((item, i) => d.appendChild(buildNode(item, r?.numbered ? `${i + 1}: ` : '')));
            } else if (r.body && typeof r.body === 'object') {
                Object.keys(r.body).forEach(k => d.appendChild(buildNode(r.body[k], `${JSON.stringify(k)}: `)));
            } else {
                d.appendChild(buildNode(r.body, ''));
            }
        }, { once: true });
        d.open = r.open;
        return d;
    } else {
        return fromHTML(`<div>${esc(label)}${esc(JSON.stringify(data))}</div>`);
    }
}

window.addEventListener('DOMContentLoaded', () => {
    // Build the nodes
    if (document.lastChild && document.lastChild.nodeType === Node.COMMENT_NODE && document.lastChild.data.trim()) {
        for (const line of document.lastChild.data.split(/\r?\n/).filter(Boolean)) {
            const unesc = line.replaceAll('&lt;', '<').replaceAll('&gt;', '>').replaceAll('&amp;', '&');
            const data = JSON.parse(unesc);
            const node = buildNode(data, 'json:');
            const label = data.label === 'CLOUD' && data.endpoint?.includes('streamGenerateContent') ? 'LLM' : data.label;
            node.classList.add('log-entry', `label-${label}`);
            document.body.appendChild(node);
            document.getElementById(`cb-${label}`).style.display = 'inline';
            if (label !== "LLM") document.getElementById('controls').style.display = 'block';
        }
    }

    // Remember checkbox state (default to LLM checked)
    const labels = Array.from(document.querySelectorAll('#controls input[type="checkbox"]')).map(cb => cb.parentElement.id.replace('cb-', ''));
    for (const label of labels) {
        const value = (label === 'LLM' && document.getElementById('controls').style.display !== 'block') || (localStorage.getItem(label) === 'true');
        document.body.classList.toggle(`show-${label}`, value);
        const cb = document.querySelector(`#cb-${label} input`);
        cb.checked = value;
        cb.addEventListener('change', () => localStorage.setItem(label, cb.checked ? 'true' : 'false'));
    }
});
