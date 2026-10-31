# Source availability & evidence research notes

Owner: data/research. Observed 2026-10-31 from the dev machine, on an IP shared with other engineers' agents.

## ClaimReview feed (keyless; Google Data Commons)

**Fetching it**
- URL: `https://storage.googleapis.com/datacommons-feeds/claimreview/latest/data.json`
  - 200 `application/json`, about 200.9 MB (`Content-Length: 200887589`), `Cache-Control: public, max-age=3600`.
  - `Last-Modified: Sat, 31 Oct 2026 08:55:28 GMT`, so it is refreshed daily. Use the `ETag` header for conditional re-download.
- Local copy (outside OneDrive): `C:/Users/lokar/.veriloop/datasets/claimreview/data.json`.
- `json.load` of the full file takes about 20–40 s and a few GB of RAM. The sync job should parse once and index into Postgres (claim text embeddings in pgvector), not load it per request.

**Shape** (counts are from this snapshot)
- Top level: `{"@context","@type":"DataFeed","name","dateModified":"2026-10-31T08:55:14Z","dataFeedElement":[...]}`.
- `dataFeedElement`: 94,063 elements.
  - Element keys: `@type:"DataFeedItem"`, `url`, `item` (list), plus `dateCreated` (85,874) or `dateModified` (8,189).
  - `item` is a **list** of 100,407 ClaimReviews in total. 99 elements have `item: null`, so guard for it.
- Each ClaimReview (`item[i]`) has:
  - `url`: the fact-check article; all 100,407 have one.
  - `author.name` and `author.url`: the reviewing publisher.
  - `claimReviewed` (99,698): the claim text, **in the fact-check's language**.
  - `datePublished` (89,566): mostly `YYYY-MM-DD`, but some values are garbage (`10-01-01`, `710-01-01`, `2923-01-02`), so validate the range.
  - `reviewRating` (99,792):
    - `alternateName` (99,636) is the **textual verdict**; use this.
    - `ratingValue`, `bestRating` and `worstRating` appear on only about 25% of reviews and are on publisher-specific scales; ignore them.
    - Optional `image` (26,350), e.g. Factly's `False.png`, and `ratingExplanation` (22,802).
  - `itemReviewed` (95,157):
    - `@type:"Claim"`, `author.name` (the claimant, often just "Social Media User"), `datePublished` (the claim date).
    - `appearance` (56,719): `[{"@type":"CreativeWork","url":...}]`, i.e. viral post URLs (FB reels, X, Instagram). Sometimes this is a Facebook *search* URL that carries the viral caption.
    - `firstAppearance` (15,798).
  - `sdPublisher`: "Google Fact Check Tools".
  - **No `inLanguage`** (every review is blank) and **no item-level image**. Detect the language from `claimReviewed`.

**Ratings and coverage**
- Rating variety (lowercased `alternateName`, top values): false 33,438; falso 11,015; misleading 7,596; fake 3,934; نادرست 1,438; pants on fire 1,315; mostly false 1,289; true 1,274; altered 756; missing context 661; half true 617; भ्रामक 436; झूठ 371; "fact crescendo rating: false" 350+239; needs context 369; hoax 226.
  - Normalise with a lexicon, not exact match (strip the "Fact Crescendo Rating:" prefix and trailing ".").
  - Note that the Tamil Fact Crescendo prefix is in Tamil.
- Recency is thin. Only 737 reviews worldwide have `datePublished` ≥ 2026-08-01 (Aug 199, Sep 227, Oct 308).
- **India coverage is skewed**. Total India-domain reviews are about 35.9k, but since 2026-07-15 there are only **213, of which 211 are Factly** (English and Telugu: 127 False, 86 Misleading).
  - All-time counts by domain:

    | Domain | Reviews |
    |---|---|
    | factly.in | 12,458 |
    | youturn.in (incl. en.) | ~7,150 |
    | factcrescendo.com (all language subdomains, some non-India) | ~10k |
    | thequint.com (+ hindi.) | 2,436 |
    | vishvasnews.com | 1,321 |
    | thip.media | 1,100 |
    | amarujala.com | 806 |
    | newsmeter.in | 153 |
    | newschecker.in | 111 |
    | dfrac.org | 24 |
    | lighthousejournalism.com | 22 |
    | boomlive.in | 5 |
    | indiatoday.in | 1 |

  - **Absent: altnews.in, pib.gov.in.**
  - So recent BOOM, Newschecker, Quint, Alt News and PIB fact-checks must come from Google News or Bing RSS, not the feed.
- Uses so far:
  - The FALSE eval rows and the known-fakes manifest were seeded from Factly entries in this feed.
  - Demo (c) Blumenthal is in the feed (Factly, "FALSE").

## Live sources (keyless)

**Google News RSS** (`news.google.com/rss/search?q=..&hl=en-IN&gl=IN&ceid=IN:en`)
- Responses: 200 `application/xml` in 0.6–2.9 s, at most 100 items.
- `<item>` holds `title` (suffixed " - Publisher"), `link` (an opaque `news.google.com/rss/articles/CBMi...` redirect, **not** the publisher URL), `pubDate`, `description` (HTML: link plus publisher; no real snippet), and `<source url="https://publisher-domain">Publisher</source>`.
- **Use `<source url>` as the publisher domain** for independence grouping.
- To decode a link to the publisher URL takes 2 extra calls. GET `news.google.com/rss/articles/<id>` gives `data-n-a-sg`/`data-n-a-ts`, then POST `/_/DotsSplashUi/data/batchexecute` with `garturlreq`. This worked every time we tried it (see the scratch script `gnd.py`), but it is undocumented and could break.
- Supports `site:` (e.g. `site:pib.gov.in` returned 30 items) and `when:7d`.
- Hindi: `hl=hi&gl=IN&ceid=IN:hi` returns up to 100 items.
- One transient connect timeout (WinError 10060) in about 30 calls. No 429 observed.

**Bing News RSS** (`bing.com/news/search?q=..&format=rss`, optional `&mkt=en-IN` / `hi-IN`)
- Responses: 200 `application/xml`, **at most 12 items**.
- `<link>` is `bing.com/news/apiclick.aspx?...&url=<publisher URL>`; **the real URL is in the `url=` param**, so no decode step is needed.
- Has a **real `<description>` snippet**, plus `<News:Source>` and `<News:Image>`.
- **Ignores `site:`** (0 items for `site:pib.gov.in`).
- Many items are MSN syndications ("X on MSN", msn.com URLs). Group them with the original publisher.
- `pubDate` is sometimes wrong (a 28-Oct article was dated 05 Jul 2026), so prefer the page's own date.
- Copyright text restricts use to personal RSS rendering. Flag this for production.

**GDELT DOC 2.0** (`api.gdeltproject.org/api/v2/doc/doc?query=..&mode=artlist&format=json`)
- **Heavy HTTP 429** on this shared IP. Body (plain text): "Please limit requests to one every 5 seconds...". We needed 3–6 attempts per success, and some queries gave up after 6 attempts of 10–60 s backoff. Latency is 10–20 s when it does answer.
- Errors come back as **HTTP 200 plain text, not JSON**: "The specified phrase is too short.", or "illegal character ... place it in quotes like "f-16"" for unquoted hyphens. Parse defensively and quote hyphenated terms.
- Response: `{"articles":[{url,url_mobile,title,seendate:"YYYYMMDDTHHMMSSZ",socialimage,domain,language,sourcecountry}]}`; an empty result is `{}`.
- Coverage window is about 3 months (`timespan` up to 3months). **Index lag: the newest article on 31 Oct was 2026-10-25T10:30Z**, so nothing from the last ~6 days appears.
- **Fact-check sites are barely indexed** (Blumenthal deepfake: 0 articles).
- Syndicated copies share a story (indiagazette.com / calcuttanews.net).
- Treat GDELT as an optional, slow, corroboration-only signal with a 1-per-6 s global token bucket and a cache. Absent means absent (never negative).

**PIB RSS**
- `https://pib.gov.in/RssMain.aspx?ModId=6&Lang=1&Regid=3` **redirects (301→302) to the Hindi feed**.
- Use `https://www.pib.gov.in/RssMain.aspx?ModId=6&Lang=1&Regid=3&reg=3`: 200 text/xml, 20 items, `title` + `link` only (**no pubDate, no description**), some mis-encoded characters.
- Links look like `PressReleaseIframePage.aspx?PRID=<n>`. The release page is fetchable, and its date is in the body as "Posted On: 28 OCT 2026 3:50PM".
- Hindi feed: `Lang=2`.
- For search, Google News `site:pib.gov.in <terms>` works better than the RSS.

**Other sources**
- **Wikidata** `wbsearchentities`: works keyless with a descriptive User-Agent (Blumenthal → Q2023708; "Girish Kalia" → none).
- **yt-dlp** (in the shared venv) extracts X/YouTube metadata keyless, e.g. the PIB Fact Check X video and YouTube Shorts. A deleted tweet returns "No video could be found in this tweet".

**Sites that block plain fetching**
- reuters.com returns 401 (and WebFetch is blocked). Use the RSS title or Yahoo Finance syndications.
- Telegraph India, The Week, WION, Zee News, NDTV Profit and NewsBytes return 403.
- ThePrint returns a Cloudflare "Just a moment" page **with HTTP 200**. Detect that body and treat it as blocked.
- DigitEye and PolitiFact need a full browser User-Agent.
- AFP Fact Check RSS returns 403.

## Fact-check coverage in Google News RSS: what works

**Query templates**
- Claim entities + `fact check` gives the best precision.
  - `Girish Kalia deepfake`: 27 items; the top 5 were BOOM, HT, Lighthouse, Newschecker, Factly.
  - `Cyclone Arnab Visakhapatnam video fact check`: 51 items.
- Add the medium word (`video`/`image`/`photo`) plus `viral`.
- Recommended order (stop when ≥2 fact-checker groups are found):
  1. `"<key entities>" fact check`
  2. `<key entities> fake OR false OR misleading OR deepfake OR "AI-generated"`
  3. `<key entities> viral video` (no fact-check words, to surface the original reporting)
  4. `site:<factchecker-domain> <entities>` for the top fact-checkers
  5. The same in Hindi with `hl=hi&gl=IN&ceid=IN:hi`, adding `फैक्ट चेक`, `सच`, `फर्जी`, `वायरल`.
- Without "fact check", genuine news (e.g. NDTV cyclone videos) ranks above fact-checks. That's fine for TRUE claims, but the stance step must tell the fact-check apart from the news.

**Treat as ClaimReview-equivalent** (match on the Google News `<source url>` domain)
- India: altnews.in, boomlive.in (+hindi., bangla.), factly.in, newschecker.in, vishvasnews.com, thequint.com/news/webqoof, indiatoday.in/fact-check, factcrescendo.com (+subdomains), newsmobile.in, digiteye.in, newsmeter.in/fact-check, thip.media, lighthousejournalism.com, factcheck.afp.com.
- Global: reuters.com/fact-check, snopes.com, politifact.com, factcheck.org, fullfact.org, leadstories.com, misbar.com, dw.com fact check.
- `data/lists/factcheckers.json` has the IFCN status of each.
- Count **PIB Fact Check** (pib.gov.in/factcheck, @PIBFactCheck on X) as a **government** debunk, not an independent fact-checker. CyberPeace and DFRAC are not IFCN signatories, so give them a lower weight.
- Organiser and OpIndia publish "Fact Check:" headlines but are not fact-checkers. Do not treat them as ClaimReview-equivalent.

**Verdict detection in titles and snippets**
- Case-insensitive, word-bounded. Look at title, then snippet.
- **Refute** (rating false):
  - English: `false`, `fake`, `fabricated`, `hoax`, `debunk(s|ed)`, `no, `, `not true`, `didn't`/`did not`, `misleading`, `misrepresent`, `old video`, `unrelated`, `falsely (shared|linked|claims)`.
  - Hindi: `फर्जी`, `फ़र्ज़ी`, `झूठा`, `झूठ`, `भ्रामक`, `गलत`, `ग़लत`, `पुराना वीडियो`, `असंबंधित`.
  - Telugu: `తప్పు`, `ఫేక్`. Bengali: `ভুয়া`, `বিভ্রান্তিকর`. Tamil: `தவறு`, `போலி`.
- **Manipulated** (MANIPULATED signal):
  - English: `deepfake`, `deep fake`, `AI-generated`, `AI generated`, `created with AI`, `AI-manipulated`, `AI-altered`, `voice clone`, `edited`, `doctored`, `morphed`, `digitally (altered|manipulated)`, `photoshopped`, `synthetic`.
  - Hindi: `डीपफेक`, `एआई से बना`, `एडिटेड`, `छेड़छाड़`.
- **Context-only** (out_of_context): `old`, `unrelated`, `from <year>`, `from <other place>`, `not from`, `shared as`.
- **Hedges** (cap stance at neutral): `may`, `could`, `likely`, `expected`, `will it`, `experts say`, `?` at the end of a headline. Hindi: `हुआ तो`, `हो सकता है`, `सकती है`, `संभव`, `अनुमान`, `उम्मीद`.
- **Pitfalls:**
  - A title that ends with "?" ("Did Rajnath Singh accuse soldiers...?") is a fact-check question, not support.
  - "Indian Army debunks..." is a refute signal from a government source.
  - Weekly-wrap or recap articles mention many claims; don't count them as a refutation of a specific claim unless the snippet matches.

## Recommendations for the evidence engineer

- **Fast path without a Google key:** ClaimReview feed index first (embed `claimReviewed`, match at ≥0.6 similarity), then Google News + Bing RSS fact-check templates, in parallel.
- **Independence grouping:**
  - Map `<source url>` / `url=` domains through `data/lists/syndication.json`.
  - MSN → original publisher; Yahoo → original wire.
  - PIB, DD and AIR → one group, `govt_india`.
  - Detect PTI/ANI/IANS in bylines or datelines only, not in photo credits.
- **Dates:** for "recent" claims, prefer the page's `datePublished` (JSON-LD) over the RSS pubDate (Bing is sometimes wrong). Allow ±1 day for IST/UTC.
- **Never depend on GDELT for demo-critical evidence.** Cache aggressively and record cassettes (TOOL_MODE=record) on demo day.
