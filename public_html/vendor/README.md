# Vendored front-end libraries

`index.html` used to load these from `tools-static.wmflabs.org` — the **legacy** `wmflabs` hostname,
which the platform has been migrating away from. Two blocking third-party requests (111 KB) before
first paint, on a domain this tool does not control.

Copied here byte-for-byte, same versions, so the page has no external dependency:

| file | version | upstream | licence |
|---|---|---|---|
| `jquery.min.js` | jQuery 3.0.0 | <https://code.jquery.com/jquery-3.0.0.min.js> | MIT |
| `jquery.noty.packaged.min.js` | jquery-noty 2.3.8 (packaged: themes included) | <https://cdnjs.cloudflare.com/ajax/libs/jquery-noty/2.3.8/packaged/jquery.noty.packaged.min.js> | MIT |

Retrieved 2026-10-23. To update: replace the file, keep the version in this table, and check the
notifications still appear (noty is used for the "preparing…" / "no high-resolution version" messages).
