# Browser regression tests

The app itself requires only Python's standard library. Browser tests use Node
and Playwright as development dependencies:

```bash
npm ci
npx playwright install --with-deps chromium
npm run test:browser
```

Tests launch Chromium against a local Python fixture serving the actual dashboard
HTML and real Server-Sent Events. No provider credentials, API calls, or live
model traffic are used. The fixture uses port 18042.

Coverage includes default context visibility, preview/search exclusions, explicit
context reveal and raw retention, streamed replies, code-tag preservation, tool
result pairing, reconnect/resume without duplication, provider filtering and
launcher hints, and actionable authentication/rate-limit/network errors.

For validation in a minimal WSL environment without sudo, missing Chromium shared
libraries were extracted into `/tmp/lab4-browser-deps`, and tests ran with
`LD_LIBRARY_PATH=/tmp/lab4-browser-deps/usr/lib/x86_64-linux-gnu npm run test:browser`.
That temporary environment workaround is not an application requirement.
