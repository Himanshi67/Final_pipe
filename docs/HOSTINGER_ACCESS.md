# Getting the pipeline through Hostinger's CDN

**The problem in one line:** the store's CDN is set to "I'm under attack", which
answers every non-browser request with a JavaScript challenge instead of the
page. That blocks the WooCommerce REST API — and search-engine crawlers.

Nothing here is a WordPress setting. **hPanel is a separate login** from
`/wp-admin`, so a WordPress administrator account cannot make this change.

---

## Who can do this

Whoever the **Hostinger hosting account** belongs to — the person who pays for
the plan. That may be you, an agency, or a colleague. Check
`hpanel.hostinger.com` with your usual email before assuming you don't have it;
plenty of people have never logged in there.

If nobody available has it, **Hostinger support can make the change for the
account holder** — there is wording to send them at the bottom.

---

## Option A — turn the mode off (recommended, ~30 seconds)

1. Sign in at **hpanel.hostinger.com** (hosting credentials, *not* WordPress)
2. **Websites** in the top navigation
3. Find `shreejeeelectronics.in` → **Dashboard**
4. In the left sidebar, **CDN** (there is a search box if it isn't visible)
5. Click **Manage** — the security settings are one level deeper, not on the
   CDN landing page
6. Open the **Security** tab
7. The security level is a **dropdown**, not an on/off switch. It currently
   reads **"I'm under attack"**. Change it to any normal level — Low, Medium or
   High all stop the JavaScript challenge.

The site stays protected at the normal levels. "I'm under attack" is the
DDoS-emergency setting that challenges *every* visitor before serving anything;
it is not meant to be left on.

Changes propagate at the CDN edge, so give it a couple of minutes.

## Option B — if the mode must stay on

There is no documented way to exempt a single path (`/wp-json/*`) from the
challenge in the hPanel UI. The CDN's **Traffic blocking** tab manages IP and
country *blocking*, not allowlisting past the challenge.

So this one needs support: ask them to allowlist the IP the pipeline runs from,
or to exempt the REST API path. Your public IP is whatever
[ifconfig.me](https://ifconfig.me) shows on the machine that runs the scripts —
note that a home connection's IP usually changes, so Option A is more durable.

---

## How to check it worked

Either of these, no coordination needed:

**In a browser** — open a private window and go to:
```
https://shreejeeelectronics.in/robots.txt
```
You should see plain text. If you see "Checking your browser before
accessing…", the mode is still on.

**From the pipeline:**
```
.venv\Scripts\python.exe wc_publish.py --check
```
Success prints the WooCommerce and WordPress versions. Note there is no
`--cookie-file` in that command — the point is that it should no longer need one.

---

## Why this matters beyond the pipeline

Hostinger's own documentation says this mode *"temporarily blocks search engine
bots, which may result in your site not being indexed during this period."*

We confirmed it: `https://shreejeeelectronics.in/robots.txt` returns **HTTP 403**
to any non-browser client. That is the first file Google, Bing and every other
crawler asks for. While this stays on, the SEO work in this pipeline —
meta titles, descriptions, image alt text, structured tags — is being produced
for crawlers that are being turned away at the door.

---

## Message to send Hostinger support

> Hello — my site **shreejeeelectronics.in** has the Hostinger CDN security
> level set to **"I'm under attack"**. I'd like it changed to a normal level
> (Low or Medium).
>
> It is currently returning HTTP 403 with the "Checking your browser" challenge
> to every non-browser request, including `/robots.txt` and the WordPress REST
> API at `/wp-json/`. This blocks a tool I use to manage my product catalogue,
> and I'm concerned it is also blocking search engine crawlers.
>
> If the security level must stay as it is, could you instead allowlist my IP
> address (………) or exempt the `/wp-json/*` path from the challenge?
>
> Thank you.

---

## Until then

The manual route is unaffected — a browser passes the challenge, so
**Media → Add New** plus **WooCommerce → Products → Import** work normally today.

The `--cookie-file` stopgap in `wc_publish.py` also still works, but measured
across six attempts a borrowed clearance lasted **minutes**, and one expired 90
seconds into a five-product push. It is fine for a handful of products and will
not carry a full batch. See KNOWN_ISSUES.md (KI-15).

Sources: [Hostinger — CDN Under Attack Mode](https://www.hostinger.com/support/8512979-hostinger-cdn-the-under-attack-mode/) ·
[Hostinger — CDN Security Levels](https://www.hostinger.com/support/10695636-hostinger-cdn-security-levels/) ·
[Hostinger — CDN help centre](https://www.hostinger.com/support/website/cdn/)
