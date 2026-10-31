# VeriLoop demo cases (SPEC §13)

Owner: data/research. Verified 2026-10-31. Every URL below was fetched on that date (curl with a browser User-Agent
plus a keyword check, or WebFetch), and the fact attributed to it was found on the page. Google News and Bing result
counts were observed on 2026-10-31 and will drift, so **re-run every query on demo day** (commands at the end).
Media for (b) and (c) is also seeded in `data/seed/known_fakes/manifest.csv` (KF-004 and KF-001).

| Case | Input | Expected | Strongest evidence |
|---|---|---|---|
| (a) TRUE | Cabinet approves 3% DA hike (28 Oct 2026) | TRUE, government source flagged | PIB + The Hindu + Financial Express + Moneycontrol |
| (b) FALSE viral | "Cyclone Arnab hitting Visakhapatnam" video (really Typhoon Bavi, July 2026) | FALSE (out of context) + report package | Newschecker + Fact Crescendo + The Quint |
| (c) DEEPFAKE | US Senator Blumenthal "India is a failed state" video | MANIPULATED + legal draft (human-gated) | BOOM + Factly + Newschecker + The Quint, plus PIB Fact Check on X |
| (d) AMBIGUOUS | "8th Pay Commission will recommend fitment factor 2.86" | UNVERIFIABLE | Only hedged speculation exists; the report is not out |

---------------------------------------------------------------------------------------------------
## (a) TRUE: Union Cabinet approves 3% DA/DR hike (28 Oct 2026)

**Input (text):** `The Union Cabinet approved a 3% hike in Dearness Allowance for central government employees and pensioners on 28 October 2026.`
Optional login-free URL input: https://www.thehindu.com/news/national/cabinet-approves-3-hike-in-dearness-allowance-for-central-government-employees/article71635503.ece

**Expected:** TRUE, with the PIB match flagged as a government source and at least 4 independent publisher groups.

| Publisher (independence group) | Date | URL | Confirms |
|---|---|---|---|
| PIB, Govt of India (govt_india) | 2026-10-28 15:50 IST | https://www.pib.gov.in/PressReleasePage.aspx?PRID=2329484 | +3% over the existing 60%, w.e.f. 01.07.2026; 48.54 lakh employees, 68.71 lakh pensioners |
| The Hindu (The Hindu Group) | 2026-10-28 | https://www.thehindu.com/news/national/cabinet-approves-3-hike-in-dearness-allowance-for-central-government-employees/article71635503.ece | "Cabinet approves 3% hike in dearness allowance..." |
| Financial Express (Indian Express Group) | 2026-10-28 | https://www.financialexpress.com/money/cabinet-approves-3-da-hike-for-central-government-employees-4371335/ | 3% increase effective 1 July 2026, from 60% |
| Moneycontrol (Network18) | 2026-10-28 | https://www.moneycontrol.com/news/business/personal-finance/cabinet-approves-3-da-hike-for-central-government-employees-allowance-rises-to-63-of-basic-pay-14062548.html | "allowance rises to 63% of basic pay" |

**Queries observed 2026-10-31:**
- Google News RSS `Cabinet approves 3% dearness allowance hike central government employees`: **92 items** (News On AIR, Business Standard, DD India, ETV Bharat and others, dated 28–29 Oct).
- Google News RSS `dearness allowance site:pib.gov.in`: **30 items**; #1 is the 28-Oct PIB release.
- Bing News RSS (same text query): **12 items** (India TV, Moneycontrol, Financial Express, The Hindu, BusinessLine, India Today/MSN). Bing **ignores `site:pib.gov.in`** (0 items).
- GDELT `"dearness allowance" sourcecountry:india` (4 weeks): 30 articles, but the **newest is 2026-10-25**, so the index lags and nothing covers the 28-Oct decision. Don't rely on GDELT for this case.

**Risks:**
- DA hikes recur twice a year. Results also include the 18 Apr 2026 central DA release and **state** DA hikes (Rajasthan, Himachal). Matching must key on "central/Union" and the date.
- Telegraph India and The Week return 403 to curl. Their RSS titles still work.

**Hindi (E9) variant:** `केंद्रीय मंत्रिमंडल ने केंद्रीय कर्मचारियों और पेंशनभोगियों के महंगाई भत्ते में 3 प्रतिशत की बढ़ोतरी को मंजूरी दी।`
- Google News `hl=hi&gl=IN&ceid=IN:hi` returns 100 items. Verified: Dainik Jagran https://www.jagran.com/news/national-central-employees-da-hiked-by-3-big-news-for-pensioners-news-in-hindi-40419599.html.
- Risk: the top two Hindi hits are *state* DA hikes.

---------------------------------------------------------------------------------------------------
## (b) FALSE viral: "Cyclone Arnab hitting Visakhapatnam" video (actually Super Typhoon Bavi, July 2026)

**Input:**
- Viral Facebook reel cited by Fact Crescendo: https://www.facebook.com/reel/1815114716474947. Not fetched; probably login-walled (exercises E2).
- **Reliable demo input:** upload the clip with the caption `This video shows Cyclone Arnab hitting Visakhapatnam, Andhra Pradesh.`
- The clip is the original Typhoon Bavi footage, downloadable with yt-dlp: https://www.youtube.com/shorts/XeYAGU1Z6l8 (34 s, 1080x1920, uploaded 2026-07-06; checked with `yt-dlp --simulate`).
- Still image of the fake: https://english.factcrescendo.com/wp-content/uploads/2026/10/Image-4-1-fact-checked.webp (200, image/webp).

**Expected:** FALSE (out of context: real footage, wrong event). The known-fake index matches KF-004, and the report package becomes eligible.
Cyclone Arnab itself is real (landfall 28 Oct 2026; eval row T-008). The *video* is what's false, which is a good test of claim framing.

| Fact-checker | Date | URL | Finding |
|---|---|---|---|
| Newschecker | 2026-10-28 | https://newschecker.in/fact-check/fact-check/old-video-shared-as-cyclone-arnab-wreaking-havoc-in-visakhapatnam | Footage is Typhoon Bavi on Rota Island, Northern Mariana Islands |
| Fact Crescendo | 2026-10-28 | https://english.factcrescendo.com/2026/10/28/old-video-goes-viral-linking-to-cyclone-arnab-in-visakhapatnam-ap/ | Misleading. Bavi landfall on Rota, 6 Jul 2026; online since July 2026 (AP Facebook, NDT News YouTube) |
| The Quint WebQoof | 2026-10-28/29 | https://www.thequint.com/news/webqoof/old-video-from-us-territory-falsely-linked-to-cyclone-arnab-visakhapatnam-fact-check | Old video from a US territory (Bavi/Rota) |

**Queries observed 2026-10-31:**
- Google News `Cyclone Arnab Visakhapatnam video fact check`: **51 items**. Top hits: Newschecker (x3), The Quint, NewsMeter, India Today.
- Google News `Cyclone Arnab Visakhapatnam old video`: **24 items** (The Quint, NewsMobile "Old Video Falsely Shared As Cyclone Arnab Wreaking Havoc In Visakhapatnam", Newschecker; also *genuine* NDTV cyclone videos).
- Bing News `Cyclone Arnab Visakhapatnam video fact check`: **11 items**, with Newschecker, Fact Crescendo and The Quint all in the top 5.
- GDELT: not useful. The fact-check sites are barely indexed and the index lags to 25 Oct.

**Risks:**
- Several *different* "Cyclone Arnab" fake videos are in circulation (Odisha tornado, Telangana visuals, AI aircraft clip). Evidence must match *this* clip; the known-fake pHash index is what pins it.
- Genuine NDTV/ToI cyclone videos rank alongside the fact-checks, so they must not be read as support for the clip.
- Bing dates the Newschecker item "05 Jul 2026" (wrong). The page date is 28 Oct.

---------------------------------------------------------------------------------------------------
## (c) DEEPFAKE of a public figure: US Senator Richard Blumenthal "India is a failed state" video

**Subject:** Richard Blumenthal, US Senator from Connecticut, **Wikidata Q2023708** (confirmed via wbsearchentities).
A public figure; the content is non-sexual.

**Input / media:**
- Viral X post cited by BOOM, Factly and Newschecker: https://x.com/PatelKhush51313/status/2112099213802869021. On 2026-10-31, yt-dlp reported "No video could be found", so it has probably been removed.
- **Obtainable copy of the deepfake:** PIB Fact Check's alert https://x.com/PIBFactCheck/status/2112600705232249021 (yt-dlp: 19 s, 800x800, uploaded 2026-10-20; the title begins "DEEPFAKE VIDEO ALERT! Pakistani propaganda accounts are circulating a digitally altered video of U.S. Senator Richard Blumenthal...").
  - Caveat: PIB's copy probably carries PIB overlays or stamps. Prefer a clean crop, or use the frame below.
- Real source footage, per The Quint: https://x.com/SenBlumenthal/status/2100359940808355904 (yt-dlp: broadcast "Russia Sanctions Bill House Passage", 2026-09-16, 1920x1080). Useful for a side-by-side.
- Still frames: https://www.boomlive.in/h-upload/2026/10/22/1058741-collapse-economy-and-military.jpg (200, image/jpeg) and https://factly.in/wp-content/uploads//2026/10/US-Senator-Thumbnail-.jpg (200, image/jpeg).
- Text input if the media fails: `Video: US Senator Richard Blumenthal says India is a failed state with a collapsing economy and military.`

**Expected:** MANIPULATED. The rule needs a manipulation ClaimReview-equivalent from at least 2 groups plus a known-fake match, which this case has (KF-001).
Legal draft is eligible because the subject is an identifiable public figure. **Never auto-sent.**

| Fact-checker | Date | URL | Finding |
|---|---|---|---|
| BOOM | 2026-10-22 | https://www.boomlive.in/fact-check/video-of-us-senator-blumenthal-calling-india-a-failed-state-is-a-deepfake-32388 | "Is A Deepfake" |
| Factly | 2026-10-23 | https://factly.in/this-video-of-u-s-senator-blumenthal-calling-india-a-failed-state-is-a-deepfake/ | Deepfake. Also in the Google ClaimReview feed, rating "FALSE" |
| Newschecker | 2026-10-24 | https://newschecker.in/fact-check/doctored-video-falsely-shows-us-senator-richard-blumenthal-calling-india-a-failed-state | Doctored video |
| The Quint WebQoof | 2026-10-24 | https://www.thequint.com/news/webqoof/us-senator-called-india-a-failed-state-altered-video-fact-check | AI-altered |
| (govt) PIB Fact Check on X | 2026-10-20 | https://x.com/PIBFactCheck/status/2112600705232249021 | "DEEPFAKE VIDEO ALERT" (title read via yt-dlp metadata) |

**Queries observed 2026-10-31:**
- Google News `Blumenthal India failed state deepfake`: **8 items**. The top 6 are BOOM, Organiser, Newschecker, Factly, The Quint and the PIB Fact Check X post.
- Bing News (same query): **3 items** (Factly, Organiser, Newschecker).
- GDELT `Blumenthal "failed state"` (1 month): **0 articles** after one 429 retry, so GDELT does not index these fact-checks.

**Risks:**
- The original X post is gone, so the demo depends on the PIB copy or the stills, both of which may carry stamps. Forensic models may score stamped frames as edited for the wrong reason.
- Organiser is not a fact-checker (RSS-affiliated weekly). Don't count it as a ClaimReview-equivalent.

**Backup (c):** the Lt Gen Girish Kalia deepfake (Arunachal "China took control").
- Debunked by BOOM, Factly, Newschecker and Lighthouse Journalism, plus Army debunk coverage in HT, ET and Outlook. Google News `Girish Kalia deepfake` returns 27 items; Bing returns 7.
- The subject has **no Wikidata item** (0 search results), which is why it is the backup. See KF-002.

---------------------------------------------------------------------------------------------------
## (d) AMBIGUOUS: 8th Pay Commission fitment factor 2.86

**Input (text):** `The 8th Pay Commission will recommend a fitment factor of 2.86 for central government employees.`

**Expected:** UNVERIFIABLE (LOW_CONFIDENCE or INSUFFICIENT). There is plenty of on-topic coverage, but all of it is speculative, and nothing refutes the claim either.
This demonstrates G8: "no decisive evidence" is not the same as "evidence says false".

**Why it is unresolved:**
- The commission has not submitted its report. Mint, 2026-10-31, explains what happens *after* the report is submitted and says the fitment factor will be a vital aspect of it: https://www.livemint.com/money/personal-finance/8th-pay-commission-what-happens-after-the-commission-submits-its-report-5-key-steps-explained-11793424939877.html
- Expert figures conflict. Mint, 2026-06-01, asks "Will it be 2.57, 2.86 or 4.0?": https://www.livemint.com/money/personal-finance/8th-pay-commission-fitment-factor-will-it-be-2-57-2-86-or-4-0-heres-what-experts-say-11780304597954.html

**Queries observed 2026-10-31:**
- Google News `8th Pay Commission fitment factor 2.86`: **100 items**, all hedged ("May Hit 2.86 If...", "Will it be 2.57, 2.86 or 4.0?", calculator tables at 2.0/2.15/2.57/2.86).
- Bing News: **12 items**, with the same framing.

**Risks:**
- A naive stance model could read "may hit 2.86" as support. Hedge cues must cap stance at neutral: may, could, likely, expected, experts say, will it be; in Hindi हुआ तो, हो सकता है, सकती है, संभव, अनुमान, उम्मीद.
- Re-check on demo day that the report still hasn't been submitted.

---------------------------------------------------------------------------------------------------
## Re-running the queries on demo day

```
https://news.google.com/rss/search?q=<urlencoded query>&hl=en-IN&gl=IN&ceid=IN:en      (Hindi: hl=hi&gl=IN&ceid=IN:hi)
https://www.bing.com/news/search?q=<urlencoded query>&format=rss                          (publisher URL = url= param of <link>)
https://api.gdeltproject.org/api/v2/doc/doc?query=<q>&mode=artlist&format=json&maxrecords=25&timespan=4weeks&sort=datedesc
```

**Needs a human before the demo:**
1. Download the (b) clip and (c) PIB copy with `yt-dlp` into `VERILOOP_HOME` (not the repo), and keep them as local fallback media.
2. Record the backup video.
3. Re-run the queries above and update the counts.
