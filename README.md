# GSMNP 900 Miler Resources
Resources for "completing the map" of all official hiking trails in [Great Smoky Mountains National Park](https://www.nps.gov/grsm/).

# Datasets & Maps
- [Great Smoky Mountains 900 Miler Club](https://smhclub.org/900-Miler-Club) - Official website. Includes Excel file with list of required trails. (My [900 Miler Club Hike Log](https://docs.google.com/spreadsheets/d/1bFKnjtmx7KK-NzT_R0nFtAiC-mHu1fw8V2mtac089SI/edit?usp=sharing) in Google Sheets is an attempt at improving the Excel file.)
- [ArcGIS Online Trail Map](https://arcg.is/PqW9W) - My original creation with elevation gain/loss data and [estimated hiking times](https://en.wikipedia.org/wiki/Tobler%27s_hiking_function). Trails, roads, and points of interest are overlaid on the current [$1 Map](https://smokieslife.org/product/backcountry-trail-map/) that was georeferenced with [Map Warper](https://mapwarper.net/maps/88180).
  - [lines_20250211.geojson](https://github.com/ericallanwest/great-smokies-circuit/blob/e2dcffbbf794bb3478057de234300c1ddd56a6e8/lines_20250211.geojson)
  - [points_20250211.geojson](https://github.com/ericallanwest/great-smokies-circuit/blob/e2dcffbbf794bb3478057de234300c1ddd56a6e8/points_20250211.geojson)
  - [dollar_map_20250108.tif](https://github.com/ericallanwest/great-smokies-circuit/blob/753dd75b4653ce025064fb0ec89667e01fff2ecf/dollar_map_20250108.tif)
  - [elevation_20250212.tif](https://github.com/ericallanwest/great-smokies-circuit/blob/2d8f7551ff452ff3e178c2b4b5207aab47e7e118/elevation_20250212.tif) - Derived from [USGS 3DEP](https://www.usgs.gov/3d-elevation-program/about-3dep-products-services) [1-meter Standard DEMs](https://github.com/ericallanwest/great-smokies-circuit/blob/2d8f7551ff452ff3e178c2b4b5207aab47e7e118/gsmnp_1m_elevation_files.csv).
- [outrageGIS Trail PDF Maps](https://www.outragegis.com/grsm/) - Excellent maps for offline use with [Avenza Maps](https://store.avenza.com/pages/app-features) app. I relied on these maps heavily for identifying likely water sources during my circuit hike.
- [NPS/USGS GSMNP 2016 Topographic Map Bundle](https://store.avenza.com/products/npsusgs-great-smoky-mountains-national-park-2016-topographic-map-bundle-great-smoky-mountains-national-park-map) - 7.5-minute quadrangle maps (1:24,000 scale) for use with Avenza Maps app.
- [USGS topoView](https://ngmdb.usgs.gov/topoview/viewer/#11/35.5906/-83.5012) - Downloadable maps from the Historical Topographic Map Collection (1947-1992) and US Topo series (2009-present). GeoPDF files are easily imported into Avenza Maps app.
- [grsm-nps.opendata.arcgis.com](https://grsm-nps.opendata.arcgis.com/search?collection=Dataset) - Authoritative park datasets provided by the National Park Service.
- [GSMNP Landforms](https://tnlandforms.us/gsmnp/) - Tom Dunigan's excellent data archive of waterfalls, peaks, balds, rock formations, sinkholes, and more. Includes trail elevation profiles and historic maps. [No longer actively maintained](https://storymaps.arcgis.com/stories/07110fa0fd70404b9f07df1ae212d1f0).

# Books
- [Hiking Trails of the Smokies](https://smokieslife.org/product/hiking-trails-of-the-smokies/) - The Brown Book. Current edition (5th) published in 2012.
- [Hiking Trails of the Great Smoky Mountains](https://smokieslife.org/product/hiking-trails-ken-wise/) - The Green Book. Current edition (2nd) published in 2014. The [original "Blue Book" edition](https://www.amazon.com/Hiking-Trails-Great-Smoky-Mountains/dp/0870499149) (1996) is also highly recommended.
- [Day Hiker's Guide to All the Trails in the Smoky Mountains](https://smokieslife.org/product/day-hikers-guide-to-all-trails/) - See the Etnier section below for more details.

# Fastest Known Times (FKTs)
There are two supported map completion FKTs recoginzed at [fastestknowntime.com](https://fastestknowntime.com/route/great-smoky-mountains-national-park-900-nc-tn). A successful self-supported map completion FKT has not yet been reported.

#### Nancy East & Chris Ford - Supported Mixed-Gender Team FKT (September 4 to October 3, 2020)
- [Gaia GPS Tracking](https://www.gaiagps.com/datasummary/folder/2fdf7a17-4d4c-4eca-a9fe-225c9c0a42a0/)
- [Tableau Visualization](https://public.tableau.com/app/profile/ericallanwest/viz/GSMNP900MilerFKT-NancyEastandChrisFord/East_Ford_FKT_Map) - My analysis of daily progress overlaid on the [$1 Map](https://smokieslife.org/product/backcountry-trail-map/).
- [Cue Sheet](https://docs.google.com/spreadsheets/d/1yewy0VBL8va5r0qMwRLIukVofc1cK5Sm7-bl1nUeyAg/edit?usp=sharing) - My turn-by-turn re-creation of FKT progress in Google Sheets.
- [GeoJSON Point File](https://github.com/ericallanwest/great-smokies-circuit/blob/b221bb94da114a0c37bf9da9d766a3713469031b/nancy_east_fkt_10.geojson) - Derived from the Gaia GPS recordings, for analysis in [QGIS](https://qgis.org/), [Atlas](https://atlas.co/), etc.
- [Chasing the Smokies Moon](https://www.hopeandfeathertravels.com/order-chasing-the-smokies-moon/) - Nancy East's memoir of the hike, published in October 2021.

#### Jeff Woody - Supported Male FKT (July 23 to August 25, 2020)
- [Strava GPS Tracking](https://www.strava.com/athletes/6948035/training/log) - Scroll to the week of July 20-26, 2020 for recordings.
- [Tableau Visualization](https://public.tableau.com/app/profile/ericallanwest/viz/GSMNP900MilerFKT-JeffWoody/Woody_FKT_Map) - My analysis of daily progress overlaid on the [$1 Map](https://smokieslife.org/product/backcountry-trail-map/).
- [Cue Sheet](https://docs.google.com/spreadsheets/d/1HPTxD4CjjyvsUSpXQSYGiy09mZQhA8TUcZfHRDxbVIM/edit?usp=sharing) - My turn-by-turn re-creation of FKT progress in Google Sheets.
- [GeoJSON Point File](https://github.com/ericallanwest/great-smokies-circuit/blob/b221bb94da114a0c37bf9da9d766a3713469031b/jeff_woody_fkt_50.geojson) - Derived from the Strava recordings, for analysis in [QGIS](https://qgis.org/), [Atlas](https://atlas.co/), etc.

# Efficient Map Completions

#### Matthew Kirk - Great Smoky Postman (December 2019 to October 2020)
- [Great Smoky Postman Summary](https://matthewkirk.blogspot.com/2020/10/great-smoky-postman-summary.html) - Blog post from October 28, 2020. Lowest known distance of 906.1 miles.
- [Tableau Visualization](https://public.tableau.com/app/profile/ericallanwest/viz/GreatSmokyPostmanProject-MatthewKirk/Postman_Map) - My analysis of daily progress overlaid on the [$1 Map](https://smokieslife.org/product/backcountry-trail-map/).
- [Cue Sheet](https://docs.google.com/spreadsheets/d/1BoVFACHH7E4-Aeu7uGRGfXZVoJuAOrNvkz2MQOgcd6k/edit?usp=sharing) - My turn-by-turn re-creation of Postman progress in Google Sheets.

#### Eric West - Great Smokies Circuit (2019-2022)
- [Tableau Visualization](https://public.tableau.com/app/profile/ericallanwest/viz/GSMNP900MilerCircuit-EricWest/West_Circuit_Map) - My analysis of daily progress overlaid on the [$1 Map](https://smokieslife.org/product/backcountry-trail-map/).
- [Cue Sheet](https://docs.google.com/spreadsheets/d/1ACHSmas1CtTb6CJ1RvFlAs-pkRSpCG2Y7gnv6m_B_QA/edit?usp=sharing) - My turn-by-turn re-creation of 900 Miler progress in Google Sheets.
- [GeoJSON Point File](https://github.com/ericallanwest/great-smokies-circuit/blob/811641e0c99a4b824dcee5040ced33c9f9aac5d4/eric_west_circuit.geojson) - Derived from my Garmin inReach Mini and phone recordings, for analysis in [QGIS](https://qgis.org/), [Atlas](https://atlas.co/), etc.
- [Permits](https://github.com/ericallanwest/great-smokies-circuit/blob/bf3dab190c896727578445fd9ba7d9cc24ce2623/eric_west_circuit_permits.pdf) - Permits issued by the Backcountry Office for the entirety of my Circuit hike.
- [completingthemap.com](https://completingthemap.com/) - My forthcoming website with details on attempting a self-supported map completion.

#### Elizabeth Etnier - Day Hiker's Guide to All the Trails in the Smoky Mountains (Book)
- [Tableau Visualization](https://public.tableau.com/app/profile/ericallanwest/viz/DayHikersGuidetoAlltheTrailsintheSmokyMountainsEtnier/Etnier_Map) - My analysis of hike-by-hike progress overlaid on the [$1 Map](https://smokieslife.org/product/backcountry-trail-map/).
- [Cue Sheet](https://docs.google.com/spreadsheets/d/1CpX0-61aN38rtjumSJ0_Rv_m-ckZb9kNNl_bq9xX3vU/edit?usp=sharing) - My hike-by-hike re-creation of 900 Miler progress in Google Sheets.
- [smokymountainshiking.com](http://smokymountainshiking.com/) - The author's website.

# Routing / Optimization
This is the area of greatest interest to those attempting an efficient map completion. The [Traveling Salesman Problem](https://en.wikipedia.org/wiki/Travelling_salesman_problem) is perhaps the most studied routing optimization problem, but the goal of the TSP is to visit points (nodes), not lines (edges/arcs). Hiking all the trails in the park is an [arc routing](https://en.wikipedia.org/wiki/Arc_routing) problem that most closely resembles the [Chinese Postman Problem](https://en.wikipedia.org/wiki/Chinese_postman_problem).

The impact of elevation change makes the trail network "windy", as it takes more time/energy to travel uphill. For a self-supported journey, the trail network is also "rural" in that roads or non-required trails can be used to more efficiently complete the map. The [Windy Rural Postman Problem (WRPP)](https://en.wikipedia.org/wiki/Arc_routing#Windy_postman_problem_and_variants) is reasonably well known in the literature and algorithms have been created to calculate optimal (or near-optimal) solutions.
 
- [Postman Problems](https://github.com/brooksandrew/postman_problems) - Python code for solving the Chinese Postman Problem (CPP) and the Rural Postman Problem (RPP). I used this to plan my Great Smokies Circuit hike.
- [ArcRoutingLibrary](https://github.com/Olibear/ArcRoutingLibrary) - Java code with solvers for many Postman Problem variants. The code is nearly 10 years old and I have not figured out how to run it on WRPP problems.
- [The Chinese-Postman-Method](https://web.archive.org/web/20210514003240/https://www-m9.ma.tum.de/graph-algorithms/directed-chinese-postman/index_en.html) - Interactive tutorial that illustrates the algorithm for solving Postman problems.
