# Vendored browser libraries

Both files are the unmodified upstream builds, pinned by version and hash.
They are committed (not loaded from a CDN) so the demo's CSP can stay
`default-src 'self'` with no third-party origins, and so a review can see
exactly what runs.

| File | Package | Version | Source | SHA-256 | License |
| --- | --- | --- | --- | --- | --- |
| `marked.esm.js` | [marked](https://github.com/markedjs/marked) | 15.0.7 | `https://cdn.jsdelivr.net/npm/marked@15.0.7/lib/marked.esm.js` | `7a7d9a521ac9384e0c3a075120a7c486cbd0c3c32cc5601bbb79a23e97403690` | MIT |
| `purify.es.mjs` | [DOMPurify](https://github.com/cure53/DOMPurify) | 3.2.4 | `https://cdn.jsdelivr.net/npm/dompurify@3.2.4/dist/purify.es.mjs` | `3b024991882ccc83066a8f0e64f3ff2a84d7638e64591221f6b04f8c10cbb3c2` | Apache-2.0 OR MPL-2.0 |

`marked` turns the assistant's Markdown answer into HTML; DOMPurify then
sanitizes that HTML before it is inserted into the page. Model output is
untrusted input: never render it any other way.

To update: change the version in both the URL and this table, re-download,
recompute the hash, and run the demo's test suite.
