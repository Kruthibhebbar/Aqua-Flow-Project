# AquaFlow Driver Operations Suite - change notes

## What changed in one paragraph

The driver portal was rebuilt as one coherent application (shell + 8 pages), the
delivery workflow and the driver login were hardened, driver documents moved
from public `/static` URLs to private, ownership-checked storage, and a
support/emergency channel that actually persists was added. The user and admin
portals are unchanged apart from two small, deliberate admin edits (document
review controls, a support-queue page).

## Security fixes (these go beyond the visual redesign - please read)

| Problem found in the original code | Fix |
|---|---|
| **Driver login OTP could be skipped.** `driver_email` was put in the session after the *password* step, and every driver route only checks for that key. | Password step now stores only `pending_driver_email`; `driver_email` is set after the OTP is verified. |
| The OTP itself sat in the (signed but readable) session cookie, with unlimited guesses. | Only a keyed HMAC is stored. Failed attempts are counted on the driver's record; 5 failures lock login for 15 minutes (the client cannot reset it by discarding cookies). |
| `/api/update-delivery-status` accepted any status from the client, **including `Delivered`**, bypassing the customer OTP. | Only `Accepted -> On The Way -> Arrived` are allowed, each from exactly one status, enforced in the database update itself. `Delivered` is refused. |
| `/verify-delivery-otp` could complete an already-delivered order again (double earnings) or complete one that never started. | Requires `On The Way`/`Arrived`; completion is a compare-and-set; wrong guesses are counted atomically so parallel requests can't exceed 5. |
| `/arrived-location` was a GET that could set *any* status (even `Delivered -> Arrived`). | POST only, valid only from `On The Way`. |
| Legacy `/accept-booking` and `/reject-booking` had no status guard (a driver could pull a delivered order back to *Accepted* or unassign it). | Guarded to `Assigned` + owner. |
| `/driver-online`, `/driver-offline` changed state on GET; `/api/driver-status` accepted any string. | GET routes removed; status must be `Online`/`Offline`. |
| Documents were saved under `static/` and served publicly. | Saved in `private_uploads/driver_documents/` under random names; served only by owner/admin routes; the old public URL prefix returns 404. Files are validated by extension, MIME, real content and size. |
| No CSRF protection anywhere. | Token required on state-changing **driver** requests (skipped only when `WTF_CSRF_ENABLED=False`, which your test fixture already sets). |
| `customer rating` defaulted to 5.0 / 4.8 and "5 mins" response time were hard-coded. | Removed. Nothing is shown unless it exists in the database. |

## New / changed routes

Pages: `/driver-dashboard`, `/driver-trips`, `/driver-earnings` (+ `/driver-earnings/statement.csv`),
`/driver-analytics`, `/driver-documents`, `/driver-profile`, `/driver-notifications`, `/driver-support`.

JSON: `/api/driver/dashboard-live`, `/api/driver/trips/<id>`, `/api/driver/analytics`,
`/api/driver/profile`, `/api/driver/profile/photo`, `/api/driver/password`,
`/api/driver/documents/upload`, `/api/driver/notifications` (+ `/<id>/read`, `/read-all`),
`/api/driver/support`, `/api/driver/support/emergency`.

Files: `/driver-documents/<id>/file` (owner), `/admin/driver-documents/<id>/file` (admin).
Admin: `POST /admin/driver-documents/<id>/approve|reject`, `/admin/driver-support` (+ `/<id>/resolve|reopen`).

Removed (they rendered templates that never existed, or were duplicated/shadowed):
`/driver-active-orders`, `/driver-completed-orders`, `/driver-order-details`, `/driver-online`,
`/driver-offline`, the duplicate `/driver-logout` and legacy `/driver-notifications`, `/update-driver-profile`, `/upload-document`.

## Database

* **New collection** `driver_support_requests` (ticket, driver, category, subject, message, priority, status, ...).
* `driver_documents` records gain `expiry_date`, `reviewed_at/by`, `rejection_reason`, `original_filename`,
  `mime_type`, `size_bytes`, `stored_name`, `expiry_notified`. Statuses: `Pending Review` -> `Verified` / `Rejected`
  (`Verified` is the word the admin page already used). `Expired` is derived from the date, never stored.
  Old records keep working.
* `drivers` gain `emergency_contact`, `otp_failed_attempts`, `otp_locked_until`, `password_changed_at`, `profile_updated_at`.
* New indexes: `bookings.rejected_by_drivers`, `driver_documents(driver_email, document_type)`, support-request indexes.
* No migration script is required.

## Configuration

| Setting | Purpose |
|---|---|
| `SUPPORT_EMAIL` (env, optional) | Support requests and emergency alerts are also emailed here. Without it they are still saved and appear in `/admin/driver-support`; the UI never claims an email was sent unless it was. |
| `DRIVER_DOCUMENTS_DIR` (Flask config, optional) | Where private documents are stored. Point it at a persistent volume in production. |
| `DRIVER_PHOTOS_DIR` (Flask config, optional) | Same for profile photos (default `static/uploads/drivers`). |

Existing documents already uploaded to `static/uploads/driver_documents/` keep working (admin and driver open them
through the protected routes) but are no longer reachable by URL. Move them into `private_uploads/driver_documents/`
whenever convenient; the stored `file_path` does not need to change if you keep the same relative path.

## Run it

```
pip install -r requirements.txt -r requirements-dev.txt
pytest
ruff check .
python Backend/app.py          # from the project root, http://127.0.0.1:5000
```

## Known limitations

* **Customer ratings and tips do not exist in the data.** `rate_delivery.html` has a form but no route stores the
  result, and nothing writes `tip_amount`. The UI therefore shows "No ratings yet" and no tips; once a rating route
  writes `customer_rating` on the booking, ratings and the rating trend appear automatically.
* The milestone ("every 25 deliveries") is shown as progress only; no code in the project credits it.
* `drivers.total_earnings` (written at OTP completion as the *full* fee, asserted by an existing test) is a legacy,
  inconsistent value. The portal never reads it; it uses the 90% commission rule shared with the admin finance code.
* `/api/gps/track/<booking_id>` (customer tracking) has no authentication in the original code and returns the driver's
  location and phone. It is outside the driver portal so it was left unchanged; consider authorising it.
* Booking references are the first 8 hex characters of the id (the convention the customer and admin pages use). Those
  characters are a timestamp, so two bookings created in the same second share a reference.
* Fonts, Font Awesome and Leaflet still load from CDNs, as in the original project. If Leaflet is blocked the map shows
  a clear fallback message.
* Real GPS hardware and real MongoDB were not available while building this - see the test report.

## Manual test checklist

**Driver:** log in (password then OTP; confirm the dashboard is unreachable before the OTP) - toggle Online/Offline and
reload - accept a request - start - mark arrived - complete with a wrong then the right OTP - open My Trips, filter,
search, open Details - Earnings: filter, download the CSV, report cash - Analytics: change ranges, enter a bad custom
range - Documents: upload a PDF, try a renamed .exe, replace a rejected document - Profile: edit phone, change photo,
change password - Notifications: mark one/all read - Support: send a request, send an emergency alert.

**Admin:** Driver Management -> open a driver's documents -> Approve / Reject (with a reason) and check the driver is
notified - `/admin/driver-support` lists the requests and can resolve them - the finance dashboard totals for a
driver match that driver's Earnings page.

**Customer:** book and track a delivery; confirm the OTP email still arrives and that the delivery only completes
with that OTP.

## Dashboard layout pass

Files: `templates/driver_dashboard.html`, `static/css/driver-suite.css` (new "DASHBOARD LAYOUT" block, scoped to `.af-dash`),
`static/js/driver-suite.js` (`AF.chart` accepts `fit: true`), `static/js/driver-dashboard.js` (chart redraws when its box resizes).

* The dashboard is a size container, so KPI columns (6 / 3 / 2) and the two-column split follow the width the page really has (sidebar open, collapsed or off-canvas), not the window width.
* Two top-aligned columns (about 61% / 39%) with natural card heights: nothing is stretched to match the other column. Card headers are one height (68px) with the action button on the right. Empty states share one compact size and are centred.
* Quick actions is always the last card of the left column (2 x 2 equal buttons).
* "Open analytics" moved into the Delivery performance header. No route, id, form, data-attribute or Jinja variable was changed.
* Anchors and focus no longer land under the sticky header (`scroll-padding-top`).
