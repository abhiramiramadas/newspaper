# Daily Abby newspaper archive update

This update turns the GitHub Pages homepage into a dated newspaper library.

## Files to replace/add in your existing `newspaper` repository

Replace these files:
- `.github/workflows/daily.yml`
- `scripts/build_issue.py`
- `app/template.html`
- `.gitignore`

Add this new file:
- `app/archive_template.html`

## What changes

- The homepage (`/`) becomes the archive, with one card per edition.
- Each edition is saved under `output/editions/YYYY-MM-DD/`.
- Every archive card links to the HTML edition and its PDF.
- Search filters by date/headline; a year dropdown filters years.
- Editions are sorted newest first.
- The workflow commits `output/` back to `main` before deploying Pages, so future Actions runs retain earlier editions. The `.gitignore` update is important: it allows the generated archive homepage (`output/index.html`) to be saved in Git.
- Re-running the workflow on the same date updates that date's issue rather than creating duplicate cards.

## Apply it

1. Download and extract `daily-abby-archive-update.zip`.
2. In your local clone of the `newspaper` repository, copy the included folders over the matching folders. Allow replacing the three existing files; keep the new `app/archive_template.html`.
3. From PowerShell in the repository folder, run:

   ```powershell
   git add .
   git commit -m "Add dated Daily Abby archive"
   git push
   ```

4. In GitHub, open **Settings → Actions → General → Workflow permissions** and choose **Read and write permissions**. Save. This is required because the workflow now commits each generated issue into `output/` so archive history survives the next run.
5. Run **Actions → Daily Abby → Run workflow**.
6. After a successful run, open your GitHub Pages site. Its homepage will show the dated archive cards.

## Important

Your current workflow has not yet produced a successful edition according to the runs shared so far. The archive will therefore start accumulating from the first successful run after this update. Failed runs do not create an archive card.
