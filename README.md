# Muriel Lake water levels

A web page showing water levels for Muriel Lake, Alberta (Water Survey of Canada station **06AC007**), updated automatically every week.

**Live page:** https://jaybeehan.github.io/muriel-lake/

## What it shows

- Every daily reading since 1981
- Levels on Jan 1, Apr 1, Jul 1 and Oct 1 each year
- Total change and year-to-year change for each of those dates (rises in green)
- A July 1 projection for next year (dry / typical / wet range) and 50-year trend lines
- A metric / inches toggle

## How it works

1. Every Monday a GitHub Action runs `scripts/build_data.py`.
2. The script downloads the approved historical record (HYDAT) and the last ~18 months of provisional real-time data from Environment Canada. Approved data always wins; real-time data only fills dates the approved record doesn't cover yet.
3. Missing days are filled by straight-line interpolation, then the quarterly tables, changes and projections are calculated and saved to `site/data/lake.json`.
4. The page in `site/` is published to GitHub Pages and draws the charts from that file.

To update right away, go to the **Actions** tab → **Update data and publish** → **Run workflow**.

## Data sources

- Approved daily means: https://api.weather.gc.ca/collections/hydrometric-daily-mean
- Real-time (provisional): https://wateroffice.ec.gc.ca/report/real_time_e.html?stn=06AC007

Data © Environment and Climate Change Canada, used under the Open Government Licence – Canada.
