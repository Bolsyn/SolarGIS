import os
from qgis.core import QgsProcessing
from qgis.core import (
    QgsProject,
    QgsPointXY,
    QgsGeometry,
    QgsCoordinateReferenceSystem,
    QgsCoordinateTransform
    )
import processing
from grass_session import Session
from grass.pygrass.modules.shortcuts import raster as r
from grass.pygrass.modules import Module

# --- 1. SETTINGS ---
BUILDINGS_LAYER_NAME   = "bratislavsky — multipolygons"
DSM_RASTER_LAYER_NAME  = "dsm_bratislava"

TARGET_LAT  = 48.154694
TARGET_LON  = 17.120704
BUFFER_SIZE_METERS = 100

PANEL_EFFICIENCY  = 0.18
PERFORMANCE_RATIO = 0.85
LINKE_TURBIDITY   = 3.0
days_to_simulate  = [80, 172, 264, 355]

print("--- BEGIN OF SCRIPT ---")

# --- 2.BUILDING SEARCH ---
project = QgsProject.instance()

def get_layer_by_name(name: str):
    layers = project.mapLayersByName(name)
    if not layers:
        raise Exception(f"Layer '{name}' didn't find'. Check the name in Layers.")
    return layers[0]

buildings_layer = get_layer_by_name(BUILDINGS_LAYER_NAME)
dsm_raster      = get_layer_by_name(DSM_RASTER_LAYER_NAME)

src_crs  = QgsCoordinateReferenceSystem("EPSG:4326")
dst_crs  = buildings_layer.crs()
transform = QgsCoordinateTransform(src_crs, dst_crs, project)
target_pt = transform.transform(QgsPointXY(TARGET_LON, TARGET_LAT))
target_geom = QgsGeometry.fromPointXY(target_pt)

found_feature = None
for f in buildings_layer.getFeatures():
    g = f.geometry()
    if g and g.contains(target_geom):
        found_feature = f
        break
if not found_feature:
    raise Exception(f"Error: Building not founded ({TARGET_LAT}, {TARGET_LON}).")

print(f"Building founded (FID: {found_feature.id()}). Preperation begining.")

# --- 3. DATA PREPARE ---
feature_geom  = found_feature.geometry()
buffer_geom   = feature_geom.buffer(BUFFER_SIZE_METERS, 8)
buffer_extent = buffer_geom.boundingBox()

home_dir = project.homePath() or os.getcwd()
clipped_dsm_path = os.path.join(home_dir, "temp_clipped_dsm.tif")

processing.run("gdal:cliprasterbyextent", {
    "INPUT": dsm_raster,
    "PROJWIN": f"{buffer_extent.xMinimum()},{buffer_extent.xMaximum()},{buffer_extent.yMinimum()},{buffer_extent.yMaximum()}",
    "OUTPUT": clipped_dsm_path
})
print(f"DSM prepeared: {clipped_dsm_path}")

# --- 3.1. CALCULATION OF ASPECT AND SLOPE ---
aspect_path = os.path.join(home_dir, "temp_aspect.tif")
slope_path = os.path.join(home_dir, "temp_slope.tif")

print("Calculation of aspect...")
processing.run("gdal:aspect", {'INPUT': clipped_dsm_path, 'OUTPUT': aspect_path})

print("Calculation of slope")
processing.run("gdal:slope", {
    'INPUT': clipped_dsm_path, 'SLOPE_EXPRESSED_IN_DEGREES': True, 'OUTPUT': slope_path
})

# --- 3.2. LAYER WITH ONE BUILDING ---
tmp_building_gpkg = os.path.join(home_dir, "temp_building.gpkg")
buildings_layer.removeSelection()
buildings_layer.select(found_feature.id())
one_building = processing.run("native:saveselectedfeatures", {
    "INPUT": buildings_layer,
    "OUTPUT": "memory:"
})["OUTPUT"]

# --- 4. CALCULATION IN GRASS ---
def rsun_pygrass(
    dsm_path, aspect_path, slope_path,
    day, linke_turbidity,
    beam_tif, diff_tif, glob_tif,
    time_of_day=12.0, nprocs=4
):
    with Session.from_raster(dsm_path):
        elev_name, aspect_name, slope_name = "elev_in", "aspect_in", "slope_in"
        beam_name, diff_name, glob_name = "beam_out", "diff_out", "glob_out"

        
        r.in_gdal(input=dsm_path, output=elev_name, overwrite=True)
        Module("g.region", raster=elev_name)

        
        if aspect_path and os.path.exists(aspect_path):
            r.in_gdal(input=aspect_path, output=aspect_name, overwrite=True)
        if slope_path and os.path.exists(slope_path):
            r.in_gdal(input=slope_path, output=slope_name, overwrite=True)
        if not os.path.exists(aspect_path) or not os.path.exists(slope_path):
            Module("r.slope.aspect", elevation=elev_name,
                   slope=slope_name, aspect=aspect_name, overwrite=True)

        
        Module("r.sun",
               elevation=elev_name,
               aspect=aspect_name,
               slope=slope_name,
               day=day,
               time=time_of_day,
               linke_value=linke_turbidity,
               beam_rad=beam_name,
               diffuse_rad=diff_name,
               glob_rad=glob_name,
               nprocs=nprocs,
               flags="i",
               overwrite=True)

        
        r.out_gdal(input=beam_name, output=beam_tif, format="GTiff", overwrite=True)
        r.out_gdal(input=diff_name, output=diff_tif, format="GTiff", overwrite=True)
        r.out_gdal(input=glob_name, output=glob_tif, format="GTiff", overwrite=True)

annual_insolation_sum_wh_m2 = 0.0

for day in days_to_simulate:
    print(f"\n=== Day {day}: calculation radiation ===")
    beam_tif = os.path.join(home_dir, f"beam_day_{day}.tif")
    diff_tif = os.path.join(home_dir, f"diff_day_{day}.tif")
    glob_tif = os.path.join(home_dir, f"glob_day_{day}.tif")

    try:
        rsun_pygrass(clipped_dsm_path, aspect_path, slope_path,
                     day, LINKE_TURBIDITY,
                     beam_tif, diff_tif, glob_tif)
    except Exception as e:
        print(f"Error in GRASS r.sun: {e}")
        continue       
    beam_layer = r["beam_rad"]
    diff_layer = r["diff_rad"]
    glob_layer = r["glob_rad"]    
 
    
    zs_beam = processing.run("native:zonalstatisticsfb", {
        "INPUT": one_building,
        "INPUT_RASTER": beam_tif,
        "RASTER_BAND": 1,
        "COLUMN_PREFIX": f"b{day}_",
        "STATISTICS": [0]
    })["OUTPUT"]

    zs_diff = processing.run("native:zonalstatisticsfb", {
        "INPUT": zs_beam,
        "INPUT_RASTER": diff_tif,
        "RASTER_BAND": 1,
        "COLUMN_PREFIX": f"d{day}_",
        "STATISTICS": [0]
    })["OUTPUT"]

    zs_glob = processing.run("native:zonalstatisticsfb", {
        "INPUT": zs_diff,
        "INPUT_RASTER": glob_tif,
        "RASTER_BAND": 1,
        "COLUMN_PREFIX": f"g{day}_",
        "STATISTICS": [0]
    })["OUTPUT"]

    feat = next(zs_glob.getFeatures())
    beam_mean = float(feat.get(f"b{day}_mean", 0) or 0)
    diff_mean = float(feat.get(f"d{day}_mean", 0) or 0)
    glob_mean = float(feat.get(f"g{day}_mean", 0) or 0)
    
    daily_global_wh_m2 = glob_mean
    annual_insolation_sum_wh_m2 += daily_global_wh_m2

    print(f"  Beam={beam_mean:.1f} Wh/m², Diff={diff_mean:.1f} Wh/m², Sum={glob_mean:.1f} Wh/m²")


# --- 5. Result ---
if annual_insolation_sum_wh_m2 > 0:
    average_daily_insolation_wh_m2 = annual_insolation_sum_wh_m2 / len(days_to_simulate)
    annual_insolation_kwh_m2       = (average_daily_insolation_wh_m2 * 365.0) / 1000.0

    building_area_m2 = feature_geom.area()
    potential_power_kwh_year = building_area_m2 * annual_insolation_kwh_m2 * PANEL_EFFICIENCY * PERFORMANCE_RATIO

    print("\n--- SOLAR POTENTIAL CALCULATION COMPLETED ---")
    print(f"Roof area (2D projection): {building_area_m2:.2f} m²")
    print(f"Average annual insolation (calculated): {annual_insolation_kwh_m2:.2f} kWh/m² per year")
    print(f"Potential energy yield: {potential_power_kwh_year:.2f} kWh per year")
    print("---------------------------------------------------------------")
else:
    print("\nERROR: Failed to calculate the total insolation. Check input data and console logs.")