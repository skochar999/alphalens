# AlphaLens — Indian Mutual Fund Analytics

AlphaLens is a data-driven mutual fund ranking platform for Indian equity mutual funds. It scores 347 actively managed funds across 21 AMCs using proprietary risk models that separate genuine manager skill from market noise.

## What this repo contains

| File | Purpose |
|---|---|
| `index.html` | The current working website — fully self-contained, open in any browser |
| `data/funds.json` | 347 fund records with scores, returns, benchmark metrics |
| `data/stats.json` | Summary statistics used in hero section |

## For Lovable — Redesign Brief

### What we want
Rebuild this as a modern React + Tailwind web app. Keep all the content, data, and functionality — improve the design and UX.

### Page structure (top to bottom)
1. **Nav** — Logo "AlphaLens", links to How it Works / Rankings
2. **Hero** — Headline + SPIVA stats (73%/82% lost to index) + philosophy box + SVG lens graphic
3. **Calculator section** — Lump Sum / SIP toggle, amount input, years slider, shows AlphaLens picks vs average fund return
4. **How AlphaLens works** — 3 cards explaining return buckets (market / style+sector / stock selection) + daily monitoring callout
5. **Proof it works** — "7 out of 10 picks beat their benchmark" with dot visual, 69% vs 27% stat boxes
6. **Fund Rankings table** — filterable by search, AMC, category, min score. Columns: Fund name, AlphaLens Score, Vs Benchmark/yr, Consistency, Stock Pick/yr, Est. Fee, Total Return/yr
7. **Fund detail drawer** — slides in from right on row click, shows charts and decomposition
8. **Methodology accordion** — FAQ section
9. **Footer** — disclaimer, ARN

### Design direction
- Light background (#F5F7FC), white cards, dark navy text
- Accent blue: #3B6EE8, Green: #059669, Red: #DC2626
- Clean, minimal, trustworthy — financial product for Indian retail investors
- Mobile responsive

### Data
Load from `data/funds.json` — each fund has: code, name, amc, cat, score, aret, hrate, pickAnn, ter, ret, navOnly, decomp, dStyle, dSector, dPick, dTiming

### Key interactions
- Table sorts by any column (default: score descending)
- Filters: search by name/AMC, filter by AMC, filter by category, filter by min score
- Click any row → open side drawer with fund details and charts
- Calculator updates live on input change

### Compliance note
Always show: "Mutual Fund investments are subject to market risks. Past performance is not indicative of future results. We are a registered Mutual Fund Distributor. Information on this site does not constitute investment advice."
