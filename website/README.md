# KubeDock Product Website

A responsive product showcase and download centre for [KubeDock](https://github.com/HareeshGT/KubeDock), built with HTML5, CSS3 and vanilla JavaScript.

## Local preview

From the repository root, start a static file server:

```bash
python3 -m http.server 8000 --directory website
```

Open <http://localhost:8000>. The download centre is at <http://localhost:8000/downloads/>.

Opening the HTML files directly with `file://` may prevent the browser from calling the GitHub Releases API. Use a local HTTP server for release integration testing.

## Features

- Responsive landing page with accessible mobile navigation.
- Product capability sections based on the repository README and security guidance.
- Animated scroll reveals that respect `prefers-reduced-motion`.
- Keyboard-operable showcase tabs and FAQ accordions.
- Downloads page backed by the public GitHub Releases REST API.
- Version-specific direct links to each release asset's `browser_download_url`.
- Release search, stable/prerelease filter, load-more pagination and session cache.
- Loading, empty, rate-limit, API-error and cached-data states.
- Safe text rendering for release notes; no remote release Markdown is injected as HTML.
- No frontend frameworks, build step, API token or custom backend.

## Release integration

The website queries:

`https://api.github.com/repos/HareeshGT/KubeDock/releases?per_page=100&page=N`

Draft releases are excluded from the public listing. Release assets are taken from the API response and linked to their actual `browser_download_url`. If GitHub is unavailable, the page uses fresh session-cached data when available and links to the canonical releases page. The platform cards are inferred from actual asset names and only shown when matching assets exist.

GitHub's anonymous API rate limits apply. The refresh button bypasses the short session cache; if the API rate limit is reached, use the linked GitHub Releases page.

## Deploy

The `website/` directory is a static site and can be deployed to GitHub Pages, Cloudflare Pages, Netlify, or another static host. Configure the hosting root to `website/`. No secret keys should be added to frontend files.

For GitHub Pages, set the Pages source to the deployment branch/folder you choose and configure the site root to the `website` directory using your preferred deployment workflow. The included paths assume the site is served from the `website/` root; if deploying under a repository subpath, configure the hosting path accordingly.

## Content accuracy

Product capabilities are based on the current repository README and security policy. The dashboard illustrations are explicitly marked as illustrative UI concepts rather than actual product screenshots. Confirm screenshots and platform packages against the live repository before making release claims.
