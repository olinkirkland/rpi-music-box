# Vendored front-end libraries

Kept in the repo so the page works with no internet and no build step.

| File | Package | Source file in the npm tarball |
|------|---------|--------------------------------|
| `vue-3.5.13.esm-browser.prod.js` | `vue@3.5.13` (MIT, `vue-LICENSE`) | `dist/vue.esm-browser.prod.js` |
| `pico-2.0.6.min.css` | `@picocss/pico@2.0.6` (MIT, `pico-LICENSE.md`) | `css/pico.min.css` |

To update: `npm pack <package>@<version>`, check the tarball against
`npm view <package>@<version> dist.integrity`, copy the file above, and
change the versioned name in `musicbox/static/index.html` / `app.js`.
