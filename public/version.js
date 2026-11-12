/*
 * The app's version: the single place to change on every release.
 * Loaded by every page and by the service worker (sw.js).
 *
 * On each release:
 *   1. Bump APP_VERSION and APP_RELEASE_DATE.
 *   2. Add a RELEASE_NOTES entry at the top, in plain language: what a person
 *      using the site will notice, 1-3 short sentences. No code details.
 *   3. Keep only the last 3 entries.
 */
self.SMT_VERSION = {
  APP_VERSION: "1.3.0",
  APP_RELEASE_DATE: "2026-11-12",
  RELEASE_NOTES: [
    {
      version: "1.3.0",
      date: "2026-11-12",
      notes: [
        "You can install the tracker as an app on your phone or computer, and it opens even without a connection.",
        "A light, dark and automatic theme switch is in the top right corner.",
        "When a new version is out, a bar at the bottom offers to update with one tap.",
      ],
    },
    {
      version: "1.2.0",
      date: "2026-11-11",
      notes: [
        "The site is much easier to use on a phone, with a menu at the top and shorter lists you can expand.",
      ],
    },
    {
      version: "1.1.0",
      date: "2026-10-11",
      notes: [
        "New Copycat backtest page: what copying the stock-picking funds would have returned against the S&P 500.",
        "A cleaner, report-style design across all pages.",
      ],
    },
  ],
};
