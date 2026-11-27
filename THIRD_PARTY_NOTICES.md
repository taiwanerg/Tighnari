# Third-party software, assets and data

Tighnari-SeisLOC's original code is AGPL-3.0-only (see LICENSE). This does not relicense third-party data or assets.

- TWERG branding: `assets/twerg_logo_clear.png` is copied from `TWERG-Bot/photos/twerg_logo_clear.png`. The logo and trademark remain with Taiwan Earthquake Recording Group. The map uses a white translucent watermark; provenance is in `assets/branding.provenance.json`.
- SeisBench: GPL-3.0, https://github.com/seisbench/seisbench . Model weights are downloaded separately from its official repository; the original PhaseNet v2 metadata states original MIT release, and geofon v2 metadata requests citation of SeisBench. Metadata/hashes are saved locally. No weights are redistributed in this project.
- PhaseNet: Zhu & Beroza, https://doi.org/10.1093/gji/ggy423 . Weights/model metadata retain upstream provenance.
- ObsPy and its dependencies: retain their respective upstream licenses; https://github.com/obspy/obspy . NumPy/SciPy, FastAPI, Uvicorn, HTTPX, Pillow, PyTorch, Shapely and pyproj remain under their own published terms.
- Noto Sans TC: SIL Open Font License 1.1. Original license is bundled in `legacy/assets/NotoSansTC-OFL.txt`; the bundled font retains its original license.
- Natural Earth: public domain, https://www.naturalearthdata.com/about/terms-of-use/ . Regional geometries are clipped/simplified; they are geographic context, not an assertion about disputed borders.
- NLSC Taiwan town boundaries: Government Data Open License v1.0. Source: https://maps.nlsc.gov.tw/pro/download.jsp . 368 administrative polygons, including the official corrected Majia/Sanhe geometry. Source/output hashes, CRS limitations and simplification details are in package `assets/geography.provenance.json`. TWD97 geographic coordinates are used as WGS84-compatible at approximately metre-level datum precision.
- EarthScope/IRIS: https://www.earthscope.org/data/terms-and-conditions/ . Cite individual networks (e.g. TW, IU, JP) and their FDSN network DOIs where applicable. Metadata and archived waveforms do not acquire the project's software license. Raw records are local downloads, not part of the distribution.
- GEOFON/GFZ: https://geofon.gfz.de/ . Provider/station availability, restrictions and network attribution remain recorded in acquisition manifests. Metadata availability alone does not establish waveform access.
- GlobalQuake historical design: public SHA c96985c86d5fb10c05d46054b0efc27d9cab7f53, MIT, Copyright 2023 xspanger3770. Public design reference.
- Shin (1993) ML, Tsuboi et al. (1995) Mwp and the other method references are listed in `README.md`. Early-est (https://doi.org/10.5194/nhess-15-2019-2015) and FilterPicker (https://doi.org/10.1785/gssrl.83.3.531) are public method references. The project uses an independent implementation.

ExpTech's historical travel table has no verified license at the referenced commit and is not bundled; existing local copies are only available through explicit legacy comparison workflows.


## Optional GDMS manual login

- Selenium: Apache-2.0, https://github.com/SeleniumHQ/selenium. Opens a visible Microsoft Edge window for a person to log in; no automatic login, Chrome attachment or data scraping.

## Optional regional CMT and W-phase research

- Pyrocko and Fomosto/QSEIS: GPLv3. The optional Pyrocko package and the QSEIS 2006b executable are installed separately. QSEIS source and license are at https://git.pyrocko.org/pyrocko/fomosto-qseis .
- Taiwan Green-function models use Wu et al. (2007), Table 1 (https://doi.org/10.1029/2007JB004983), and Pyrocko's default ak135-f density and attenuation. The model provenance is written into the locally generated stores.
- Geoscience Australia's ga-wphase is used through a standalone offline adapter with a separately installed backend and W-phase Green-function database. Upstream source and license are at https://github.com/GeoscienceAustralia/ga-wphase .
