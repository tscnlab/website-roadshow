# Nachtmensch oder Frühaufsteher? — website archive

A static preservation copy of <https://nachtmensch-oder-fruehaufsteher.de/>, hosted on GitHub Pages at <https://roadshow.tscnlab.org/>. The original German content and design are retained, with an archive notice, local assets, local PDF downloads, and disabled submission forms.

## Deliverables

- `docs/index.html`: archived home page; **publish only `docs/`**.
- `archive.qmd`: reproducible Quarto notebook that rebuilds and checks the site offline.
- `docs/archive/index.html`: rendered notebook and public archive documentation.
- `snapshot/manifest.json`: original URLs, timestamps, HTTP metadata and SHA-256 checksums.
- `snapshot/files/`: unchanged original responses, retained for reproducibility.
- `scripts/archive_site.py`: capture, offline build and validation commands.
- `.github/workflows/pages.yml`: GitHub Pages deployment workflow.

## View locally

```sh
python3 -m http.server 8000 --directory docs
```

Open <http://localhost:8000/>. Use HTTP rather than opening an HTML file directly, so the original script loaders and PDF viewer work normally.

## Rebuild from the saved snapshot

Install Python 3.11+ and Quarto, then:

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
QUARTO_PYTHON="$PWD/.venv/bin/python" quarto render
.venv/bin/python scripts/archive_site.py validate
```

Rendering `archive.qmd` verifies original checksums, rebuilds `docs/`, and produces the archive report. Rendering never contacts the original site. The rendered site is included so deployment does not need Quarto or access to the original domain.

## Publish on GitHub Pages

Repository: <https://github.com/tscnlab/website-roadshow>

Website: <https://roadshow.tscnlab.org/>

GitHub Pages uses **GitHub Actions** as its publishing source, with the custom domain
`roadshow.tscnlab.org` saved in **Settings → Pages**. DNS must retain the CNAME
`roadshow.tscnlab.org → tscnlab.github.io`. Enable **Enforce HTTPS** once GitHub's
certificate is available.

Push to `main` or run **Publish archive to GitHub Pages** manually from the Actions
page. The workflow validates all local references and source-content preservation,
then uploads only `docs/`. A separate deployment job checks the configured custom
domain and publishes that validated artifact. Pull requests run validation without
publishing. The Python runtime and parser versions are explicitly selected.

The workflow publishes the committed HTML; after editing the notebook, banner, or
archive builder, run `quarto render` as documented above and commit the regenerated
`docs/` files alongside the sources. The original-site snapshot is never refreshed
by the publishing workflow.

For this Actions-based deployment, the repository's Pages settings control the
custom domain; GitHub ignores `CNAME` files in the artifact. `.nojekyll` is included
for compatibility with static hosting. Relative links support the custom domain
and a GitHub Pages project path.

See [GitHub's custom-domain documentation](https://docs.github.com/en/pages/configuring-a-custom-domain-for-your-github-pages-site/managing-a-custom-domain-for-your-github-pages-site).

## Capture or resume

```sh
.venv/bin/python scripts/archive_site.py capture
```

This is the only command that fetches the original website. It follows public sitemaps and same-origin page/asset links with four concurrent downloads, retries transient failures, and records unavailable URLs. Existing successful downloads are reused; it does not refresh them. Preserve or move the existing `snapshot/` before starting a deliberately new dated capture. Rebuild into an empty `docs/` when changing snapshots to avoid retaining files from an older capture.

## Archive limitations and rights

The published archive has no WordPress backend, live forms, analytics or site search. The press flipbook is replaced with a local PDF viewer. External links remain external. Original legal pages are retained as historical source material, not rewritten as legal statements for the new hosting. Review the notebook's scope and capture-failure report before publication.

Original content and third-party assets retain their existing copyright and licenses; this project does not relicense them. This is a static capture, not a backup of the WordPress database or a WARC recording.
