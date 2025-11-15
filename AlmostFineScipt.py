import os
import sys
import time

from qgis.core import (
    QgsProject,
    QgsPointXY,
    QgsGeometry,
    QgsCoordinateReferenceSystem,
    QgsCoordinateTransform,
    QgsApplication,
    QgsRasterLayer
)
import processing

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

# Reproject buildings to projected CRS if needed
if buildings_layer.crs().isGeographic():
    print(f"Buildings layer in geographic CRS ({buildings_layer.crs().authid()}), reprojecting to EPSG:32634...")
    buildings_layer = processing.run("native:reprojectlayer", {
        "INPUT": buildings_layer,
        "TARGET_CRS": QgsCoordinateReferenceSystem("EPSG:32634"),
        "OUTPUT": "memory:"
    })["OUTPUT"]
    print(f"Buildings reprojected to {buildings_layer.crs().authid()}")

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

warp_res = processing.run("gdal:warpreproject", {
    "INPUT": dsm_raster,
    "TARGET_CRS": dst_crs,
    "RESAMPLING": 0,
    "OUTPUT": os.path.join(home_dir, "temp_dsm_projected.tif")
})
reprojected_dsm_path = warp_res["OUTPUT"]
# Wait briefly for file creation; fallback to original DSM if missing
for _ in range(40):
    if os.path.exists(reprojected_dsm_path):
        break
    time.sleep(0.1)
input_for_clip = reprojected_dsm_path if os.path.exists(reprojected_dsm_path) else dsm_raster
if not os.path.exists(reprojected_dsm_path):
    print("Warning: DSM reprojection file missing, clipping original DSM.")
clip_res = processing.run("gdal:cliprasterbyextent", {
    "INPUT": input_for_clip,
    "PROJWIN": f"{buffer_extent.xMinimum()},{buffer_extent.xMaximum()},{buffer_extent.yMinimum()},{buffer_extent.yMaximum()}",
    "OUTPUT": clipped_dsm_path
})
# Use actual output path if different
if not os.path.exists(clipped_dsm_path):
    clipped_dsm_path = clip_res.get("OUTPUT", clipped_dsm_path)
print(f"DSM prepeared: {clipped_dsm_path}")

# --- 3.1. CALCULATION OF ASPECT AND SLOPE ---
aspect_path = os.path.join(home_dir, "temp_aspect.tif")
slope_path = os.path.join(home_dir, "temp_slope.tif")

print("Calculation of aspect...")
aspect_res = processing.run("gdal:aspect", {
    'INPUT': clipped_dsm_path,
    'OUTPUT': aspect_path
})
aspect_path = aspect_res.get('OUTPUT', aspect_path)

print("Calculation of slope")
slope_res = processing.run("gdal:slope", {
    'INPUT': clipped_dsm_path,
    'SLOPE_EXPRESSED_IN_DEGREES': True,
    'OUTPUT': slope_path
})
slope_path = slope_res.get('OUTPUT', slope_path)

# --- 3.2. LAYER WITH ONE BUILDING ---
buildings_layer.removeSelection()
buildings_layer.select(found_feature.id())
one_building = processing.run("native:saveselectedfeatures", {
    "INPUT": buildings_layer,
    "OUTPUT": "memory:"
})["OUTPUT"]

# --- 3.3. TERRAIN ANALYSIS FOR BUILDING ROOF ---
print("\nAnalyzing roof terrain characteristics...")

# Wait for slope and aspect files to be created
for _ in range(20):
    if os.path.exists(slope_path) and os.path.exists(aspect_path):
        break
    time.sleep(0.1)

if not os.path.exists(slope_path) or not os.path.exists(aspect_path):
    print("Warning: Slope or aspect files not found, using default values")
    roof_slope_deg = 0.0
    roof_aspect_deg = 180.0
else:
    # Extract slope statistics for building footprint
    zs_slope = processing.run("native:zonalstatisticsfb", {
        "INPUT": one_building,
        "INPUT_RASTER": slope_path,
        "RASTER_BAND": 1,
        "COLUMN_PREFIX": "slope_",
        "STATISTICS": [2],  # Mean
        "OUTPUT": "memory:"
    })["OUTPUT"]

    # Extract aspect statistics for building footprint
    zs_aspect = processing.run("native:zonalstatisticsfb", {
        "INPUT": zs_slope,
        "INPUT_RASTER": aspect_path,
        "RASTER_BAND": 1,
        "COLUMN_PREFIX": "aspect_",
        "STATISTICS": [2],  # Mean
        "OUTPUT": "memory:"
    })["OUTPUT"]

    # Get mean slope and aspect values
    feat_terrain = next(zs_aspect.getFeatures())
    roof_slope_deg = float(feat_terrain["slope_mean"] if feat_terrain["slope_mean"] is not None else 0)
    roof_aspect_deg = float(feat_terrain["aspect_mean"] if feat_terrain["aspect_mean"] is not None else 180)

print(f"  Roof slope: {roof_slope_deg:.1f}°")
print(f"  Roof aspect: {roof_aspect_deg:.1f}° (0°=N, 90°=E, 180°=S, 270°=W)")

# --- 3.4. SHADOW ANALYSIS (HILLSHADE) ---
print("\nCalculating shadow factor...")
hillshade_path = os.path.join(home_dir, "temp_hillshade.tif")

# Generate hillshade for typical sun position (azimuth=180° south, altitude=45°)
hs_res = processing.run("gdal:hillshade", {
    'INPUT': clipped_dsm_path,
    'AZIMUTH': 180,
    'ALTITUDE': 45,
    'OUTPUT': hillshade_path
})
hillshade_path = hs_res.get('OUTPUT', hillshade_path)

# Wait for hillshade file creation
for _ in range(20):
    if os.path.exists(hillshade_path):
        break
    time.sleep(0.1)

if not os.path.exists(hillshade_path):
    print("Warning: Hillshade file not created, using default shadow factor")
    shadow_factor = 1.0
else:
    # Extract hillshade statistics for building
    zs_hillshade = processing.run("native:zonalstatisticsfb", {
        "INPUT": one_building,
        "INPUT_RASTER": hillshade_path,
        "RASTER_BAND": 1,
        "COLUMN_PREFIX": "hs_",
        "STATISTICS": [2],  # Mean
        "OUTPUT": "memory:"
    })["OUTPUT"]

    feat_hs = next(zs_hillshade.getFeatures())
    hillshade_mean = float(feat_hs["hs_mean"] if feat_hs["hs_mean"] is not None else 255)

    # Convert hillshade (0-255) to shadow factor (0.5-1.0)
    # Higher hillshade = less shadow, lower hillshade = more shadow
    shadow_factor = 0.5 + (hillshade_mean / 255.0) * 0.5  # Range: 0.5 (full shadow) to 1.0 (no shadow)

print(f"  Shadow factor: {shadow_factor:.2f} (1.0=no shadow, 0.5=heavily shadowed)")

# --- 4. SOLAR RADIATION CALCULATION ---
import math

def calculate_solar_declination(day):
    """Calculate solar declination for given day of year"""
    return 23.45 * math.sin(math.radians((360/365) * (day - 81)))

def calculate_daylight_hours(latitude, declination):
    """Calculate daylight hours for given latitude and declination"""
    lat_rad = math.radians(latitude)
    decl_rad = math.radians(declination)
    
    cos_omega = -math.tan(lat_rad) * math.tan(decl_rad)
    cos_omega = max(-1, min(1, cos_omega))  # Clamp to valid range
    omega_s = math.acos(cos_omega)
    
    return (2 * omega_s * 12) / math.pi

def calculate_extraterrestrial_radiation(day, latitude, declination):
    """Calculate extraterrestrial radiation (H0) in Wh/m²/day"""
    solar_constant = 1367  # W/m²
    dr = 1 + 0.033 * math.cos(math.radians(360 * day / 365))
    
    lat_rad = math.radians(latitude)
    decl_rad = math.radians(declination)
    
    cos_omega = -math.tan(lat_rad) * math.tan(decl_rad)
    cos_omega = max(-1, min(1, cos_omega))
    omega_s = math.acos(cos_omega)
    
    H0 = (24 * 3600 / math.pi) * solar_constant * dr * (
        omega_s * math.sin(lat_rad) * math.sin(decl_rad) +
        math.cos(lat_rad) * math.cos(decl_rad) * math.sin(omega_s)
    ) / 3600
    
    return H0

def calculate_terrain_correction(slope_deg, aspect_deg, latitude):
    """Calculate terrain correction factor based on slope and aspect"""
    slope_rad = math.radians(slope_deg)
    aspect_rad = math.radians(aspect_deg)
    
    # Aspect correction: south (180°) is optimal in northern hemisphere
    aspect_correction = math.cos(aspect_rad - math.radians(180))
    
    # Slope correction: optimal slope ≈ latitude
    optimal_slope = abs(latitude)
    slope_diff = abs(slope_deg - optimal_slope)
    slope_correction = 1.0 + (0.15 * math.cos(math.radians(slope_diff))) - 0.15
    
    # Combined terrain factor
    terrain_factor = 1.0 + (aspect_correction * 0.1 * math.sin(slope_rad))
    terrain_factor = terrain_factor * slope_correction
    terrain_factor = max(0.5, min(1.3, terrain_factor))  # Clamp 50%-130%
    
    return terrain_factor

def calculate_daily_radiation(day, latitude, slope_deg=0, aspect_deg=180, shadow_factor=1.0, linke_turbidity=3.0):
    """Calculate daily solar radiation with terrain and atmospheric corrections"""
    
    # 1. Solar geometry
    declination = calculate_solar_declination(day)
    daylight_hours = calculate_daylight_hours(latitude, declination)
    
    # 2. Extraterrestrial radiation
    H0 = calculate_extraterrestrial_radiation(day, latitude, declination)
    
    # 3. Atmospheric correction
    atmospheric_transmissivity = 0.75  # Clear sky
    linke_factor = 1.0 / (1.0 + 0.1 * (linke_turbidity - 3.0))
    global_radiation_horizontal = H0 * atmospheric_transmissivity * linke_factor
    
    # 4. Terrain correction
    terrain_factor = calculate_terrain_correction(slope_deg, aspect_deg, latitude)
    
    # 5. Final radiation with all corrections
    global_radiation = global_radiation_horizontal * terrain_factor * shadow_factor
    
    print(f"  Solar model: Decl={declination:.1f}°, Daylight={daylight_hours:.1f}h, Slope={slope_deg:.1f}°, Aspect={aspect_deg:.1f}°, Terrain={terrain_factor:.2f}, Shadow={shadow_factor:.2f}, Radiation={global_radiation:.1f} Wh/m²/day")
    
    return global_radiation

# --- 5. MAIN CALCULATION LOOP ---
annual_insolation_sum_wh_m2 = 0.0

for day in days_to_simulate:
    print(f"\n=== Day {day}: Calculating solar radiation ===")
    
    # Calculate daily radiation with terrain correction
    daily_global_wh_m2 = calculate_daily_radiation(
        day=day,
        latitude=TARGET_LAT,
        slope_deg=roof_slope_deg,
        aspect_deg=roof_aspect_deg,
        shadow_factor=shadow_factor,
        linke_turbidity=LINKE_TURBIDITY
    )
    
    annual_insolation_sum_wh_m2 += daily_global_wh_m2


# --- 6. RESULTS CALCULATION AND OUTPUT ---
if annual_insolation_sum_wh_m2 > 0:
    average_daily_insolation_wh_m2 = annual_insolation_sum_wh_m2 / len(days_to_simulate)
    annual_insolation_kwh_m2       = (average_daily_insolation_wh_m2 * 365.0) / 1000.0

    # Calculate area in projected CRS
    building_area_m2 = feature_geom.area()
    
    # If area is unrealistic, try to get from reprojected layer
    if building_area_m2 < 1.0 or building_area_m2 > 10000000:  # Less than 1m² or more than 10km²
        print(f"Warning: Area calculation issue (area={building_area_m2:.2f} m²), using feature from reprojected layer")
        # Get feature from one_building layer which is in projected CRS
        feat_proj = next(one_building.getFeatures())
        building_area_m2 = feat_proj.geometry().area()
    
    potential_power_kwh_year = building_area_m2 * annual_insolation_kwh_m2 * PANEL_EFFICIENCY * PERFORMANCE_RATIO

    print("\n--- SOLAR POTENTIAL CALCULATION COMPLETED ---")
    print(f"Building CRS: {buildings_layer.crs().authid()}")
    print(f"Roof area (2D projection): {building_area_m2:.2f} m²")
    print(f"Roof characteristics: Slope={roof_slope_deg:.1f}°, Aspect={roof_aspect_deg:.1f}°, Shadow={shadow_factor:.2f}")
    print(f"Average annual insolation (calculated): {annual_insolation_kwh_m2:.2f} kWh/m² per year")
    print(f"Potential energy yield: {potential_power_kwh_year:.2f} kWh per year")
    print(f"Panel efficiency: {PANEL_EFFICIENCY*100:.0f}%, Performance ratio: {PERFORMANCE_RATIO*100:.0f}%")
    print(f"Using model: Solar geometry with terrain and shadow correction")
    print("---------------------------------------------------------------")
else:
    print("\nERROR: Failed to calculate the total insolation. Check input data and console logs.")