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

TMP = QgsProcessing.TEMPORARY_OUTPUT
# --- 1. НАСТРОЙКИ ---
BUILDINGS_LAYER_NAME   = "bratislavsky — multipolygons"
DSM_RASTER_LAYER_NAME  = "dsm_bratislava"

TARGET_LAT  = 48.154694
TARGET_LON  = 17.120704
BUFFER_SIZE_METERS = 100

PANEL_EFFICIENCY  = 0.18
PERFORMANCE_RATIO = 0.85
LINKE_TURBIDITY   = 3.0
days_to_simulate  = [80, 172, 264, 355]

print("--- НАЧАЛО РАБОТЫ СКРИПТА ---")

# --- 2. ПОИСК СЛОЁВ И ОБЪЕКТА ---
project = QgsProject.instance()

def get_layer_by_name(name: str):
    layers = project.mapLayersByName(name)
    if not layers:
        raise Exception(f"Слой '{name}' не найден. Проверьте точное имя в панели Layers.")
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
    raise Exception(f"Ошибка: Не удалось найти здание по координатам ({TARGET_LAT}, {TARGET_LON}).")

print(f"Здание найдено (FID: {found_feature.id()}). Начинаем подготовку.")

# --- 3. ПОДГОТОВКА ДАННЫХ ---
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
print(f"DSM обрезан до области интереса: {clipped_dsm_path}")

# --- 3.1. ПРЕДВАРИТЕЛЬНЫЙ РАСЧЕТ АСПЕКТА И УКЛОНА ---
aspect_path = os.path.join(home_dir, "temp_aspect.tif")
slope_path = os.path.join(home_dir, "temp_slope.tif")

print("Расчет растра аспекта (ориентации склонов)...")
processing.run("gdal:aspect", {'INPUT': clipped_dsm_path, 'OUTPUT': aspect_path})

print("Расчет растра уклона...")
processing.run("gdal:slope", {
    'INPUT': clipped_dsm_path, 'SLOPE_EXPRESSED_IN_DEGREES': True, 'OUTPUT': slope_path
})

# --- 3.2. СОЗДАНИЕ СЛОЯ С ОДНИМ ЗДАНИЕМ ---
tmp_building_gpkg = os.path.join(home_dir, "temp_building.gpkg")
buildings_layer.removeSelection()
buildings_layer.select(found_feature.id())
one_building = processing.run("native:saveselectedfeatures", {
    "INPUT": buildings_layer,
    "OUTPUT": "memory:"  # вместо сохранения в файл
})["OUTPUT"]

# --- 4. РАСЧЕТ В GRASS ---
annual_insolation_sum_wh_m2 = 0.0

for day in days_to_simulate:
    print(f"День {day}: расчёт прямой и рассеянной радиации...")
    beam_tif = os.path.join(home_dir, f"temp_beam_day_{day}.tif")
    diff_tif = os.path.join(home_dir, f"temp_diff_day_{day}.tif")
    glob_tif = os.path.join(home_dir, f'temp_global_rad_day_{day}.tif')
    
    res = None
    
    try:
        res = processing.run("grass:r.sun.incidout", {
            "elevation": clipped_dsm_path,
            "aspect": aspect_path,          # можно поставить None, r.sun сам возьмет из DEM
            "slope": slope_path,            # можно поставить None
            "dayofyear": day,
            "time": 12.0,                   # обязателен оберткой; при 'i' не влияет
            "linke": LINKE_TURBIDITY,
            "beam_rad": beam_tif,
            "diff_rad": diff_tif,
            "refl_rad": None,
            "glob_rad": glob_tif,   # это уже прямая+рассеянная (+отраженная, если задавать)
            "flags": "i",                   # интеграция за день → Wh/m²
            "nprocs": 4,
            "GRASS_REGION_PARAMETER": None,
            "GRASS_REGION_CELLSIZE_PARAMETER": 0,
            "GRASS_RASTER_FORMAT_OPT": "",
            "GRASS_RASTER_FORMAT_META": ""
            })
    except Exception as e:
        print("!!! ПРОИЗОШЛА КРИТИЧЕСКАЯ ОШИБКА ПРИ ВЫЗОВЕ АЛГОРИТМА GRASS !!!")
        print(f"Текст ошибки: {e}")
        print("Пожалуйста, убедитесь, что GRASS активирован в 'Настройки -> Обработка -> Провайдеры'.")
        raise
    
    # если результат не получен — пропускаем день
    if not res:
        print(f"⚠️ GRASS не вернул результат для дня {day}, пропуск...")
        continue

    # Проверяем наличие ключей
    missing = [k for k in ("beam_rad", "diff_rad", "glob_rad") if k not in res or not res[k]]
    if missing:
        print(f"⚠️ Пропущены ключи {missing} в результате GRASS — день {day} пропущен.")
        continue
    
    beam_layer = res["beam_rad"]
    diff_layer = res["diff_rad"]
    glob_layer = res["glob_rad"]     # основной результат
    
    zs_beam = processing.run("native:zonalstatisticsfb", {
        "INPUT": one_building,
        "INPUT_RASTER": beam_tif,
        "RASTER_BAND": 1,
        "COLUMN_PREFIX": f"b{day}_",
        "STATISTICS": [0]  # mean
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

    eat = next(zs_glob.getFeatures())
    beam_mean = float(feat.get(f"b{day}_mean", 0) or 0)
    diff_mean = float(feat.get(f"d{day}_mean", 0) or 0)
    glob_mean = float(feat.get(f"g{day}_mean", 0) or 0)
    
    annual_insolation_sum_wh_m2 += daily_global_Wh_m2
    print(f"  Прямая={beam_mean:.1f} Вт·ч/м², Рассеянная={diff_mean:.1f} Вт·ч/м², Сумма={daily_global_wh_m2:.1f} Вт·ч/м²")

# --- 5. ИТОГОВЫЙ РАСЧЁТ ---
if annual_insolation_sum_wh_m2 > 0:
    average_daily_insolation_wh_m2 = annual_insolation_sum_wh_m2 / len(days_to_simulate)
    annual_insolation_kwh_m2       = (average_daily_insolation_wh_m2 * 365.0) / 1000.0

    building_area_m2 = feature_geom.area()
    potential_power_kwh_year = building_area_m2 * annual_insolation_kwh_m2 * PANEL_EFFICIENCY * PERFORMANCE_RATIO

    print("\n--- РАСЧЁТ СОЛНЕЧНОГО ПОТЕНЦИАЛА ЗАВЕРШЁН ---")
    print(f"Площадь крыши (2D проекция): {building_area_m2:.2f} м²")
    print(f"Среднегодовая инсоляция (расчетная): {annual_insolation_kwh_m2:.2f} кВт·ч/м² в год")
    print(f"Потенциальная выработка: {potential_power_kwh_year:.2f} кВт·ч в год")
    print("-----------------------------------------------------------------")
else:
    print("\nОШИБКА: Не удалось рассчитать итоговую инсоляцию. Проверьте входные данные и логи в консоли.")