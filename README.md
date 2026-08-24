# LMSW Remote NY Job Finder — GitHub Actions edition

Migrated from a local `launchd`/`.env` setup to run in the cloud via GitHub
Actions, so it fires every day at **9:00 PM Eastern** whether or not your
laptop is on.

Nothing about the search logic changed — same sources (SerpAPI Google Jobs,
Indeed HTML, LinkedIn, Idealist), same LMSW/remote/NY filtering, same
`sent_jobs.db` dedup database (carried over from your existing one, so you
won't get a flood of "new" postings you've already seen).

## What changed under the hood
- The script now reads its config from **environment variables only**
  (GitHub Actions secrets) instead of a local `.env` file — `.env` still
  works if you ever run it locally too, nothing was removed there.
- Added a **time gate**: since GitHub's cron scheduler only understands UTC
  and doesn't know about Daylight Saving Time, the workflow fires two cron
  triggers a day (one for EDT, one for EST) and the script checks the actual
  local time in `America/New_York` before doing any work, so you get exactly
  one email a day, not two.
- Manual test runs (clicking "Run workflow" in the Actions tab) always run
  immediately, bypassing the time gate.

## One-time setup

### 1. Create the GitHub repo and push
Same process as the condo monitor repo:
```bash
cd lmsw-job-finder
git init
git add .
git commit -m "Initial commit — migrated to GitHub Actions"
git branch -M main
gh repo create lmsw-job-finder --private --source=. --remote=origin --push
```
(If you don't have the `gh` CLI set up, use `git remote add origin <url>` and
`git push -u origin main` instead — you'll hit the same auth flow as before.)

### 2. Add repo secrets
Repo → **Settings → Secrets and variables → Actions → New repository secret**.
Add each of these (reuse your existing values from the old `.env` file):

| Secret name | Value |
|---|---|
| `GMAIL_USER` | your Gmail address |
| `GMAIL_APP_PASSWORD` | your 16-character Gmail app password |
| `RECIPIENT_EMAIL` | where the digest should go (can be same as GMAIL_USER) |
| `SERPAPI_KEY` | your SerpAPI key (optional — script works without it, just with less coverage) |
| `SEND_EMPTY_DIGEST` | `true` or `false` |

You can reuse the same Gmail app password from the condo monitor project if
it's the same sending account — app passwords aren't tied to one repo.

### 3. Test it
Actions tab → **Daily LMSW Job Digest** → **Run workflow**. Check your inbox,
and check the run logs if anything fails — same troubleshooting playbook as
the condo monitor (missing secret, blank secret value, Gmail auth, etc.).

## Worth knowing: scraping from a cloud runner is less reliable than from home

LinkedIn's guest job-search endpoint and Indeed's HTML search pages are two
of your four sources, and both actively try to detect and block bot traffic.
Requests from a residential home IP (what the script used before) tend to
fly under the radar more easily than requests from GitHub Actions' shared
runner IP ranges, which belong to a known cloud provider and get blocked or
rate-limited more aggressively by sites doing bot detection.

Practically: don't be surprised if `search_linkedin` or `search_indeed_html`
return zero results on some days even when postings exist, more often than
they did running locally. SerpAPI and Idealist's JSON API aren't affected by
this since they're legitimate API integrations rather than HTML scraping.
If LinkedIn/Indeed coverage matters a lot to you, leaning more on SerpAPI
(which itself aggregates Indeed, LinkedIn, ZipRecruiter, and others through
Google Jobs) is the more cloud-reliable path — worth watching the first
couple weeks of runs to see how much it actually affects results.

## Files in this repo

| File | Purpose |
|---|---|
| `lmsw_job_finder.py` | Main script |
| `requirements.txt` | Python dependencies |
| `sent_jobs.db` | SQLite dedup database — updated and committed after every run |
| `.github/workflows/daily-job-digest.yml` | The schedule + run steps |
| `.gitignore` | Keeps `.env` and log files out of the repo |
