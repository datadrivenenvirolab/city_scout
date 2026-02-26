from functools import reduce
import json
import logging
from pprint import pprint
import spacy
from geopy import geocoders
import country_converter as coco
from pprint import pprint, pformat

logger = logging.getLogger(__name__)

nlp = None
country_province_names = None


def load_province_names() -> dict:
    PROVINCE_NAME_FILES = "countries_province_names.json"
    if not country_province_names:
        with open(PROVINCE_NAME_FILES, "r+", encoding="utf-8") as countries_json_file:
            country_province_names = json.load(countries_json_file)

    return countries_json_file


def read_regions_data() -> dict:
    pass


def get_country_iso(country_name: str) -> str:
    cc = coco.CountryConverter()

    some_countries = [country_name]
    
    iso3_codes = cc.convert(names=some_countries, to='ISO3')      


    # print(iso3_codes)
    # exit()
    return iso3_codes

    ISO_NAMES_FILE = "iso_country_codes.json"
    with open(ISO_NAMES_FILE, "r+", encoding="utf-8") as json_file:
        isos_dict = json.load(json_file)

    country_iso = isos_dict.get(country_name, None)

    if country_iso is None:
        country_iso = isos_dict.get(fuzzy_match_names(country_name, isos_dict.keys()))
    return country_iso


def get_region_types_by_country_eng(country_name: str | None, country_iso: str | None) -> list:
    ENTITIES_FILE_NAME = "iso_entities.json"

    with open(ENTITIES_FILE_NAME, "r+", encoding="utf-8") as region_json_file:
        region_dict = json.load(region_json_file)

    if not country_iso:
        country_iso = get_country_iso(country_name)
    regions_country_by_iso = region_dict[country_iso]

    regions_eng = list()
    for region in regions_country_by_iso:
        regions_eng.append(region["ent_type_eng"])

    return regions_eng


def get_region_types_by_country(country_name: str | None = None, country_iso: str | None = None) -> list:
    # print("GT",country_name)
    logger.info("For " + str(country_name) + " " + str(country_iso))
    ENTITIES_FILE_NAME = "iso_entities.json"

    with open(ENTITIES_FILE_NAME, "r+", encoding="utf-8") as region_json_file:
        region_dict = json.load(region_json_file)

    if not country_iso:
        # print(country_name, country_iso)

        country_iso = get_country_iso(country_name)

        logger.info("Got ISO: " + str(country_name) + " " + str(country_iso))

    # pprint(region_dict.keys())


    regions_country_by_iso = region_dict[country_iso]

    # pprint(regions_country_by_iso)

    regions_eng = list()
    for region in regions_country_by_iso:
        regions_eng.append(region["ent_type_local"])

    # exit()

    return regions_eng


def get_region_right_name() -> str:
    pass


def get_region_right_name_full() -> str:
    pass


def fuzzy_match_names(name: str, list_to_match_from: list) -> str:
    global nlp
    if not nlp:
        nlp = spacy.load("en_core_web_md")

    # # list_to_match_from = ["United States of America", "United Kingdom", "India"]
    # name = "US"?

    doc_query = nlp(name)
    similarities = [(country, doc_query.similarity(nlp(country))) for country in list_to_match_from]

    # similarities = sorted(similarities, key=lambda x: x[1], reverse=True)
    # pprint(similarities)

    best_match = reduce(lambda a, b: a if a[1] >= b[1] else b, similarities)[0]
    print(name, best_match)

    # logging.info("")
    return best_match

def get_address(search_dict):
    query_parts = []
    for key in search_dict.keys():
        val = search_dict.get(key)
        # print(type(val), val)
        if val and str(val).strip() != "":
            query_parts.append(str(val) )
    geocoders.GoogleV3
    gn = geocoders.Nominatim(user_agent="report_fetcher")
    place, (lat, lng) = gn.geocode(" ".join(query_parts))
    # print(place)
    return place

def print_to_logger(*varaible):
    # logger.info()
    # with open("run_cities.log", "a+", encoding="utf-8") as logFile:
    #     logFile.write("\n".join([str(pformat(var)) for var in varaible]))

    return "\n".join([str(pformat(var)) for var in varaible])
    