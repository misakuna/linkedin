# web/ — David's dashboard

`index.html` is the entire front end. One self-contained file: markup, CSS,
JavaScript, and the LinkedIn logo assets inlined as data URIs. **No build step,
no framework, no dependencies, no external requests.**

That is deliberate. Big buttons and plain language do not need a bundler, and a
single file means the thing David depends on daily has no build that can break
between a working commit and a deploy.

## How it runs

Two ways, same file:

| Mode | Data shown | How |
| --- | --- | --- |
| **Design work** | Demo data | Open `index.html` from disk in a browser |
| **Served** | The real database | `app/dashboard/routes.py` serves it |

The only difference is one flag. The file on disk says `const USE_MOCK = true;`
so it stays openable; `load_dashboard_html()` rewrites that to `false` at boot.

Because it is read **once at startup**, editing this file does nothing to a
running server until you restart it. That reliably trips people up during
design tweaks.

## Editing it

The app is a `class`, so its methods take **no comma separators**. Adding one is
a syntax error that kills the whole script — the page still renders, but no
button works and the login screen never advances. It looks like a CSS problem
and it isn't.

Check that before trusting a screenshot:

```bash
python - <<'PY'
import re
from pathlib import Path
s = re.findall(r"<script>(.*?)</script>", Path("web/index.html").read_text(), re.DOTALL)[-1]
Path("/tmp/app.js").write_text(s)
PY
node --check /tmp/app.js
```

## What used to be here

The Vite/React/Tailwind scaffolding this folder shipped with has been removed —
the stub component was empty, none of the declared dependencies were imported,
and no build ever ran. It only implied a pipeline that did not exist.

`linkedin-logo/` stays: those are the source PNGs the inlined assets and the
favicon were generated from.
