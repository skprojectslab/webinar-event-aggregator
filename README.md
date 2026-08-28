# Public Webinar & Event Aggregator

This is the hosted/public version. It is designed for an office laptop where you cannot install Node/Python packages.

## How it works

GitHub's hosted runner runs the scraper every 6 hours and can also be started manually from the Actions tab. The generated `data/events.json` and static dashboard are then deployed to GitHub Pages.

Your office laptop only needs a normal web browser.

## Official sources only

- techUK
- AWS Connected Community
- AWS Events & Webinars
- Microsoft Events
- NASSCOM
- Google Cloud Events
- IBM Events
- IBM Research Events
- TechMarketView (TMV)

No third-party event aggregators are configured.

## Deployment

1. Create a **public GitHub repository**.
2. Upload the contents of this folder to the repository's `main` branch.
3. In GitHub: **Settings → Pages → Source → GitHub Actions**.
4. Open **Actions → Refresh events and publish → Run workflow**.
5. After the workflow succeeds, GitHub Pages provides the public URL.

The scheduled job runs every 6 hours. The manual workflow lets you refresh it whenever you want.

## Important

The dashboard is public. The source code and event data are also public because this design uses a public repository and GitHub Pages.

GitHub Pages is intended for static sites; the scraper runs in GitHub Actions rather than in the browser. GitHub's documentation confirms that Pages can be deployed through custom GitHub Actions workflows, and scheduled workflows are supported. 
